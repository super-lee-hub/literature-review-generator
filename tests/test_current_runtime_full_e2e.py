from __future__ import annotations

import configparser
import copy
import hashlib
import json
import re
import threading
import zipfile
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import fitz  # type: ignore
import pytest

import ai_interface
import reviewctl
from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from services.job_outcome import load_canonical_job_outcome
from services.writer_wire_projection import expand_writer_source_bundles_for_test_v1
from summary_schema import normalize_ai_summary
from validation.closure import resolve_current_stage_closure_map
from validation.disposition import ValidationDispositionV1


_PAPER_LEVEL_STUDY_LIMITATION_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "run_first_paper_level_study_limits.json").read_text(
        encoding="utf-8"
    )
)
_PAPER_LEVEL_LIMITATION_CLAIMS = {
    str(claim_id): dict(row)
    for claim_id, row in _PAPER_LEVEL_STUDY_LIMITATION_FIXTURE.get("claims", {}).items()
    if isinstance(row, Mapping)
}

_PAPER_STUDY_LABEL_PATTERN = re.compile(
    r"\b(?P<label>studies|study)\s+(?P<sequence>\d+[A-Za-z]?(?:\s*(?:and|,|&|or|to|through|[-–—])\s*\d+[A-Za-z]?)*)"
    r"(?P<possessive>['’]s)?(?!\w)",
    flags=re.IGNORECASE,
)
_PAPER_STUDY_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
}


def _rewrite_paper_level_study_labels(source_text: str) -> str:
    """Keep source wording intact except its explicitly paper-level study labels."""

    def render(match: re.Match[str]) -> str:
        label = str(match.group("label") or "").casefold()
        sequence = str(match.group("sequence") or "")
        prior_text = source_text[:match.start()].rstrip()
        source_prefix = "The paper's" if not prior_text or prior_text.endswith((".", "?", "!")) else "the paper's"
        parts = re.split(r"(\s*(?:and|,|&|or|to|through|[-–—])\s*)", sequence)
        rendered_parts: list[str] = []
        number_values: list[tuple[int, str]] = []
        for part in parts:
            if not part:
                continue
            token = re.fullmatch(r"(\d+)([A-Za-z]?)", part.strip())
            if token is not None:
                number = int(token.group(1))
                suffix = token.group(2)
                ordinal = _PAPER_STUDY_ORDINALS.get(number)
                if ordinal is None:
                    raise AssertionError("paper-level study label is outside the captured ordinal range")
                number_values.append((number, suffix))
                rendered_parts.append(ordinal + (f" (part {suffix})" if suffix else ""))
            else:
                if part.strip().casefold() in {"-", "–", "—", "to", "through"}:
                    rendered_parts.append(" through ")
                else:
                    rendered_parts.append(part)
        rendered_sequence = "".join(rendered_parts).strip()
        possessive = str(match.group("possessive") or "")
        if label == "study" and len(number_values) == 1:
            ordinal, suffix = rendered_parts[0], number_values[0][1]
            suffix_text = f" (part {suffix})" if suffix else ""
            return f"{source_prefix} {ordinal.split(' (part ', 1)[0]} study{suffix_text}{possessive}"
        return f"{source_prefix} {rendered_sequence} studies{possessive}"

    return _PAPER_STUDY_LABEL_PATTERN.sub(render, source_text)


def _source_attributed_paper_level_limitation(
    source_claim: Mapping[str, Any],
    *,
    selected_study_id: str = "",
    selected_paper_key: str = "",
    paper_alias_map: Mapping[str, Any] | None = None,
) -> str:
    """Paraphrase only verified paper-level limitation/boundary labels."""

    claim_id = str(source_claim.get("claim_id") or "")
    source_claim_text = str(source_claim.get("text") or "")
    rule = _PAPER_LEVEL_LIMITATION_CLAIMS.get(claim_id)
    if rule is None:
        return source_claim_text
    allowed_locators = {
        "stage1:ai_summary.limitations",
        "stage1:ai_summary.moderators_boundaries",
    }
    if (
        str(source_claim.get("claim_type") or "") != "author_interpretation"
        or str(source_claim.get("source_locator") or "") not in allowed_locators
        or str(rule.get("claim_type") or "") != "author_interpretation"
        or str(source_claim.get("source_locator") or "") != str(rule.get("source_locator") or "")
        or str(rule.get("source_locator") or "") not in allowed_locators
    ):
        raise AssertionError("paper-level label rewrite is outside the limitation/boundary source class")
    expected_paper_key = str(rule.get("paper_key") or "")
    alias_snapshot = _PAPER_LEVEL_STUDY_LIMITATION_FIXTURE.get("paper_alias_map_snapshot")

    def canonical_source_owner(observed: str) -> str:
        if observed == expected_paper_key:
            return observed
        aliases = paper_alias_map
        if aliases is None and isinstance(alias_snapshot, Mapping):
            aliases = alias_snapshot
        if not isinstance(aliases, Mapping):
            return observed
        # The fixture stores the selected forward/reverse rows. Its
        # payload_sha256 is copied from the full durable alias-map artifact;
        # it is provenance for that artifact, not a digest recomputed over
        # this selected-row excerpt. Validate the exact reversible pair used
        # here and make no broader alias-map authority claim.
        if (
            str(aliases.get("artifact_type") or "") != "outline_evidence_alias_map"
            or str(aliases.get("artifact_version") or "") != "v1"
            or str(aliases.get("payload_sha256") or "")
            != str(alias_snapshot.get("payload_sha256") or "")
        ):
            raise AssertionError("paper-level limitation alias map differs from its verified fixture binding")
        papers = aliases.get("papers")
        papers_reverse = aliases.get("papers_reverse")
        if not isinstance(papers, Mapping) or not isinstance(papers_reverse, Mapping):
            raise AssertionError("paper-level limitation alias map is missing its reversible identity rows")
        canonical = str(papers_reverse.get(observed) or "")
        if canonical != expected_paper_key or str(papers.get(expected_paper_key) or "") != observed:
            raise AssertionError("paper-level limitation claim was attributed to a different source paper")
        return canonical

    source_owner = canonical_source_owner(str(source_claim.get("paper_key") or ""))
    selected_owner = (
        canonical_source_owner(str(selected_paper_key or ""))
        if selected_paper_key
        else source_owner
    )
    if source_owner != expected_paper_key or selected_owner != expected_paper_key:
        raise AssertionError("paper-level limitation claim was attributed to a different source paper")
    if (
        str(selected_study_id or "")
        or str(source_claim.get("study_id") or "")
        or tuple(str(value) for value in source_claim.get("owner_study_ids") or () if str(value))
        or str(rule.get("study_id") or "")
    ):
        raise AssertionError("paper-level limitation cannot acquire a study-level owner")
    if tuple(str(value) for value in source_claim.get("owner_study_ids") or ()) != tuple(
        str(value) for value in rule.get("owner_study_ids") or ()
    ):
        raise AssertionError("paper-level limitation owner sentinel differs from typed source")
    source_text = str(rule.get("source_text") or "")
    if not source_text or source_claim_text != source_text:
        raise AssertionError("paper-level limitation text differs from its persisted typed source claim")
    if str(source_claim.get("claim_type") or "") != str(rule.get("claim_type") or ""):
        raise AssertionError("paper-level limitation claim type differs from its typed source")
    if str(source_claim.get("source_summary_hash") or "") != str(rule.get("source_summary_hash") or ""):
        raise AssertionError("paper-level limitation source summary binding changed")
    observed_evidence = sorted(
        str(value) for value in source_claim.get("evidence_ids") or () if str(value)
    )
    expected_evidence = sorted(str(value) for value in rule.get("evidence_ids") or () if str(value))
    if not expected_evidence or observed_evidence != expected_evidence:
        raise AssertionError("paper-level limitation evidence closure differs from its typed source")
    if (
        not _PAPER_STUDY_LABEL_PATTERN.search(source_text)
        or hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        != str(rule.get("source_text_sha256") or "")
    ):
        raise AssertionError("paper-level limitation fixture has an unknown numbered-study phrase")
    # The section and support row already bind this statement to paper_key.
    # Rewording only the exact numeric label avoids asserting a verified study
    # ownership record that is absent from the typed inventory. The saved
    # source text/hash and exact claim/evidence IDs remain the oracle.
    return _rewrite_paper_level_study_labels(source_text)


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


def _fixture_semantic_output_for_contract(
    request: Mapping[str, Any],
    content: Mapping[str, Any],
) -> dict[str, Any]:
    """Honor the shared contract's local coverage ledger on fixture output."""

    if request.get("shared_synthesis_contract_version") != "v1":
        return dict(content)
    return {
        key: value
        for key, value in content.items()
        if key not in {
            "processed_topic_ids",
            "processed_fragment_ids",
            "processed_result_ids",
            "processed_relation_ids",
        }
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
    if len(field_texts) != len(source_field_ids):
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

    def append_source_closed_claim(
        fragment_id: str,
        record: Mapping[str, Any],
        closed: Mapping[str, Any],
        supported: list[dict[str, Any]],
    ) -> None:
        source_claim = record["claim"]
        paper_key = str(record["paper_key"])
        source_claim_ids = list(closed["source_claim_ids"])
        local_digest = hashlib.sha256(
            (fragment_id + "|" + paper_key + "|" + str(closed["primary_claim_id"])).encode("utf-8")
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
            "synthesis_claim_id": str(synthesis_claim["claim_id"]),
            "fragment_id": fragment_id,
            "primary_claim_id": closed["primary_claim_id"],
            "source_claim_ids": list(closed["source_claim_ids"]),
            "source_claim_records": [
                {
                    "claim_id": source_claim_id,
                    "paper_key": paper_key,
                    "claim_type": str(
                        record["claims_by_id"][source_claim_id].get("claim_type") or ""
                    ),
                    "source_locator": str(
                        record["claims_by_id"][source_claim_id].get("source_locator") or ""
                    ),
                    "text": str(record["claims_by_id"][source_claim_id].get("text") or ""),
                }
                for source_claim_id in closed["source_claim_ids"]
                if isinstance(record.get("claims_by_id"), Mapping)
                and isinstance(record["claims_by_id"].get(source_claim_id), Mapping)
            ],
            "evidence_ids": list(closed["evidence_ids"]),
            "source_field_ids": list(closed["source_field_ids"]),
            "qualifier_source_claim_ids": list(closed["qualifier_source_claim_ids"]),
            "qualifier_evidence_ids": list(closed["qualifier_evidence_ids"]),
            "qualifier_source_field_ids": list(closed["qualifier_source_field_ids"]),
            "dependency_rows": [dict(item) for item in closed["dependency_rows"]],
            "dependency_ids": sorted({
                str(item.get("dependency_id") or "")
                for item in closed["dependency_rows"]
                if str(item.get("dependency_id") or "")
            }),
        })
        supported.append(synthesis_claim)
        used_source_claims.update(source_claim_ids)
        covered_papers.add(paper_key)

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
            selected_by_paper: dict[str, tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]] = {}
            for candidate in sorted(candidates, key=lambda row: row[0]):
                paper_key = str(candidate[1].get("paper_key") or "")
                if paper_key and paper_key not in selected_by_paper:
                    selected_by_paper[paper_key] = candidate
            for paper_key in sorted(selected_by_paper):
                _rank, record, closed = selected_by_paper[paper_key]
                append_source_closed_claim(fragment_id, record, closed, supported)
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
        topic_members_by_id: dict[str, set[str]] = {}
        for topic in topics:
            topic_id = str(topic.get("topic_id") or "")
            if not topic_id:
                continue
            topic_members_by_id.setdefault(topic_id, set()).update(
                str(value)
                for value in (
                    *list(topic.get("paper_ids") or ()),
                    *list(topic.get("bridge_paper_ids") or ()),
                )
                if str(value)
            )
        fixture_trace.append({
            "kind": "topic_source_bound",
            "node_id": node_id,
            # Preserve the exact request ownership through the scale fixture's
            # compact cross-group fold. A topic's bridge papers are explicit
            # request membership, not a group-level union to infer later.
            "topic_members_by_id": {
                key: sorted(value) for key, value in sorted(topic_members_by_id.items())
            },
            "claims": [
                {
                    "claim_id": claim["claim_id"],
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

    from tests.test_run_first_scale_integration import _noncomparison_paper_role

    task_dimensions = {
        str(route.get("logical_node_id") or ""): {
            str(value).strip().casefold()
            for value in route.get("dimensions") or ()
            if str(value).strip()
        }
        for route in topic_routes if isinstance(route, Mapping)
    }

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
    if not evidence_rows or not claims_by_paper:
        raise AssertionError("actual candidate request lacks complete source-bound interpretation inputs")
    evidence_rows.sort(key=lambda item: str(item.get("paper_key") or ""))
    titles_by_paper: dict[str, str] = {}
    for evidence in evidence_rows:
        paper_key = str(evidence.get("paper_key") or "").strip()
        title = str(evidence.get("title") or "").strip()
        if not paper_key or not title:
            raise AssertionError("candidate evidence omitted its canonical paper key or source title")
        previous_title = titles_by_paper.get(paper_key)
        if previous_title and previous_title != title:
            raise AssertionError(f"candidate evidence has conflicting source titles for {paper_key}")
        titles_by_paper[paper_key] = title
    output_scope = request.get("candidate_output_scope")
    paper_keys_by_group_index: dict[int, set[str]] = {}
    scope_slots = [
        item for item in output_scope.get("claim_slots") or ()
        if isinstance(item, Mapping)
    ] if isinstance(output_scope, Mapping) else []
    for slot in scope_slots:
        group_index = slot.get("claim_group_index")
        paper_key = str(slot.get("paper_key") or "").strip()
        if type(group_index) is int and paper_key:
            paper_keys_by_group_index.setdefault(group_index, set()).add(paper_key)
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
        if not (claim_ids and evidence_ids and primary_evidence_ids):
            raise AssertionError(f"source-bound candidate claim has incomplete primary or qualifier closure for {paper_key}")
        text = _candidate_source_clause(titles_by_paper[paper_key], str(chosen["text"]))
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
        if isinstance(output_scope, Mapping):
            matching_slots = [
                slot for slot in scope_slots
                if isinstance(slot, Mapping) and slot.get("paper_key") == paper_key
                and slot.get("primary_claim_id") == primary_id
                and slot.get("synthesis_claim_id") == chosen.get("claim_id")
                and type(slot.get("claim_group_index")) is int
                and paper_keys_by_group_index.get(slot.get("claim_group_index")) == {paper_key}
            ]
            if not matching_slots:
                # A source claim that exists only inside a multi-paper output
                # group must stay in that shared assertion, with its sibling
                # member, instead of being emitted as a standalone claim.
                continue
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
            "paper_roles": {paper_key: _noncomparison_paper_role([support], task_dimensions)},
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
    # Corpus witnesses and explicit comparisons are complete assertion groups,
    # so selecting one topic claim per paper cannot replace their member slots.
    if isinstance(output_scope, Mapping):
        source_claims = {
            str(row.get("claim_id") or ""): row
            for row in tables.get("source_claims") or () if isinstance(row, Mapping)
        }
        used_slots = {
            str(row["claim_slot_id"])
            for section in sections for row in section["claim_support"]
        }
        for group_index in output_scope.get("required_claim_group_indices") or ():
            group_slots = [
                row for row in output_scope.get("claim_slots") or ()
                if isinstance(row, Mapping) and row.get("claim_group_index") == group_index
            ]
            if type(group_index) is not int or not group_slots:
                raise AssertionError("candidate fixture required group has no complete support slots")
            group_slot_ids = {str(row["claim_slot_id"]) for row in group_slots}
            if group_slot_ids.issubset(used_slots):
                continue
            if group_slot_ids & used_slots:
                raise AssertionError("candidate fixture split a required group across sections")
            group_support: list[dict[str, Any]] = []
            source_statements: list[str] = []
            for slot in sorted(group_slots, key=lambda row: str(row["claim_slot_id"])):
                paper_key = str(slot["paper_key"])
                primary_id = str(slot["primary_claim_id"])
                applicable = [
                    row for row in dependencies
                    if row.get("paper_key") == paper_key and row.get("primary_claim_id") == primary_id
                ]
                source_ids = set(slot["source_claim_ids"])
                evidence_ids = set(slot["evidence_ids"])
                field_ids = set(slot["source_field_ids"])
                qualifier_ids = {
                    str(value) for row in applicable for value in row.get("required_source_claim_ids") or ()
                }
                qualifier_evidence = {
                    str(value) for row in applicable for value in row.get("required_evidence_ids") or ()
                }
                qualifier_fields = {
                    str(value) for row in applicable for value in row.get("required_source_field_ids") or ()
                }
                if not (qualifier_ids <= source_ids and qualifier_evidence <= evidence_ids
                        and qualifier_fields <= field_ids):
                    raise AssertionError("candidate fixture required group lost its qualifier closure")
                pieces: list[str] = []
                for source_id in [primary_id, *sorted(source_ids - {primary_id})]:
                    source_claim = source_claims.get(source_id)
                    if not isinstance(source_claim, Mapping) or source_claim.get("paper_key") != paper_key:
                        raise AssertionError("candidate fixture required group lost a source-owned claim")
                    text = _source_attributed_paper_level_limitation(
                        source_claim, selected_study_id=str(slot.get("study_id") or ""),
                        selected_paper_key=paper_key,
                    ).strip().rstrip(".。;； ")
                    if not text:
                        raise AssertionError("candidate fixture required group has no source claim text")
                    if text.casefold() not in "; ".join(pieces).casefold():
                        pieces.append(text)
                for field_id in sorted(field_ids):
                    field = source_fields.get(field_id)
                    if not field or field.get("paper_key") != paper_key:
                        raise AssertionError("candidate fixture required group lost a source-owned field")
                    value = str(field.get("source_value") or "").strip().rstrip(".。;； ")
                    if value and value.casefold() not in "; ".join(pieces).casefold():
                        pieces.append(value)
                source_statements.append(
                    _candidate_source_clause(titles_by_paper[paper_key], "; ".join(pieces))
                )
                primary_evidence = set(source_claims[primary_id].get("evidence_ids") or ())
                primary_evidence.update(
                    value for row in applicable for value in row.get("primary_evidence_ids") or ()
                )
                if not primary_evidence or not primary_evidence <= evidence_ids:
                    raise AssertionError("candidate fixture required group lost its primary evidence")
                support = {
                    key: slot[key] for key in (
                        "claim_slot_id", "task_id", "paper_key", "primary_claim_id",
                        "source_claim_ids", "evidence_ids", "source_field_ids",
                    )
                }
                support.update({
                    "primary_evidence_ids": sorted(primary_evidence),
                    "qualifier_source_claim_ids": sorted(qualifier_ids),
                    "qualifier_evidence_ids": sorted(qualifier_evidence),
                    "qualifier_source_field_ids": sorted(qualifier_fields),
                    "dependency_ids": sorted(str(row["dependency_id"]) for row in applicable),
                    **({"study_id": slot["study_id"]} if slot.get("study_id") else {}),
                })
                group_support.append(support)
                trace_sections.append(dict(support))
            text = "Within the recorded source boundaries, " + "; whereas ".join(source_statements) + "."
            for support in group_support:
                support["claim"] = text
            sections.append({
                "section_id": f"{candidate_id}_required_group_{group_index}",
                "title": f"{organizing_logic.replace('_', ' ')} source comparison {group_index}",
                "goal": "Retain the required source group and each member's recorded qualification.",
                "paper_keys": sorted({row["paper_key"] for row in group_support}),
                "relation_ids": sorted({value for slot in group_slots for value in slot.get("relation_ids") or ()}),
                "claims": [text], "claim_support": group_support,
                "paper_roles": {
                    paper: (
                        "Compare its source-reported claim with "
                        + ", ".join(
                            titles_by_paper[counterpart]
                            for counterpart in sorted(
                                {row["paper_key"] for row in group_support} - {paper}
                            )
                        )
                        + " within their recorded source boundaries."
                        if len({row["paper_key"] for row in group_support}) > 1
                        else _noncomparison_paper_role(
                            [row for row in group_support if row["paper_key"] == paper], task_dimensions,
                        )
                    )
                    for paper in sorted({row["paper_key"] for row in group_support})
                },
                "task_ids": sorted({row["task_id"] for row in group_support}),
                "rationale": "Compare the source-reported claims with their complete source-owned support.",
            })
            used_slots.update(group_slot_ids)
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


def _candidate_source_clause(title: str, source_text: str) -> str:
    """Attribute canonical source text to its exact evidence-row title."""

    source_title = str(title or "").strip()
    statement = re.sub(r"[\s.!?。！？；;]+$", "", str(source_text or "")).strip()
    if not source_title or not statement:
        raise AssertionError("candidate fixture cannot emit a source clause without its title and claim")
    if source_title.casefold() in statement.casefold():
        raise AssertionError("candidate source claim already contains its title and cannot be anchored uniquely")
    return f"{source_title} reports {statement}."


def _single_sentence_writer_claim(claim: str) -> str:
    """Join authored claim sentences without dropping source clauses."""

    from services.sentence_segmenter import segment_sentences

    spans = segment_sentences(str(claim or ""))
    if not spans:
        return ""

    closing_punctuation = frozenset("\"'”’」』】）》〉)]}）")

    def strip_sentence_terminator(value: str) -> str:
        text = value.rstrip()
        closing: list[str] = []
        while text and text[-1] in closing_punctuation:
            closing.append(text[-1])
            text = text[:-1].rstrip()
        while text and text[-1] in ".!?。！？；;":
            text = text[:-1].rstrip()
        return text + "".join(reversed(closing))

    clauses = [strip_sentence_terminator(span.raw_text) for span in spans]
    if any(not clause.strip() for clause in clauses):
        raise AssertionError("Writer fixture could not preserve an empty sentence clause")
    return "; ".join(clauses)


def _writer_source_statement_signature(bundles: list[dict[str, Any]], paper: str) -> tuple[str, ...]:
    """Compare complete source semantics while preserving row multiplicity."""
    signatures = []
    for bundle in bundles:
        if bundle.get("paper_key") != paper:
            continue
        evidence = {row["evidence_id"]: {key: value for key, value in row.items() if key != "evidence_id"}
                    for row in bundle["evidence"]}
        fields = {
            row["source_field_id"]: {
                key: str(value).replace(paper, "{paper}") if key in {"study_id", "source_study_id"} else value
                for key, value in row.items() if key not in {"source_field_id", "source_summary_hash"}
            }
            for row in bundle["source_fields"]
        }
        claims = {
            row["claim_id"]: {
                "claim_type": row["claim_type"], "text": row["text"],
                "study_id": str(row.get("study_id") or "").replace(paper, "{paper}"),
                "evidence": sorted((evidence[ref] for ref in row["evidence_ids"]), key=lambda value: json.dumps(value, sort_keys=True)),
            }
            for row in bundle["source_claims"]
        }
        dependencies = []
        for row in bundle["interpretation_dependencies"]:
            semantic = {key: value for key, value in row.items() if key not in {
                "primary_claim_id", "required_source_claim_ids", "required_evidence_ids", "required_source_field_ids",
            }}
            for key in ("study_id", "owner_study_id"):
                if key in semantic:
                    semantic[key] = str(semantic[key]).replace(paper, "{paper}")
            semantic["primary"] = claims[row["primary_claim_id"]]
            for key, entities in (("required_source_claim_ids", claims), ("required_evidence_ids", evidence),
                                  ("required_source_field_ids", fields)):
                semantic[key] = sorted((entities[ref] for ref in row[key]), key=lambda value: json.dumps(value, sort_keys=True))
            dependencies.append(semantic)
        signatures.append(json.dumps({
            "study_id": str(bundle.get("study_id") or "").replace(paper, "{paper}"),
            "claims": sorted(claims.values(), key=lambda value: json.dumps(value, sort_keys=True)),
            "evidence": sorted(evidence.values(), key=lambda value: json.dumps(value, sort_keys=True)),
            "fields": sorted(fields.values(), key=lambda value: json.dumps(value, sort_keys=True)),
            "dependencies": sorted(dependencies, key=lambda value: json.dumps(value, sort_keys=True)),
        }, sort_keys=True, ensure_ascii=False))
    return tuple(sorted(signatures))


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
    catalog_entries = [
        dict(item)
        for item in envelope.get("citation_ref_catalog") or ()
        if isinstance(item, Mapping)
    ]
    blocks: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    task_trace: list[dict[str, Any]] = []
    canonical_bundles: dict[str, list[dict[str, Any]]] = {}
    if any(task.get("canonical_source_bundle_refs") for task in tasks):
        semantic_contract = scope.get("provider_semantic_contract_version")
        source_projection = scope if semantic_contract is not None else {
            **dict(scope), "schema_version": "writer_task_scope_wire/v1",
        }
        canonical_bundles = expand_writer_source_bundles_for_test_v1(
            source_projection, provider_semantic_contract_version=semantic_contract,
        )
    for task in tasks:
        task_id = str(task.get("writer_task_id") or "")
        if not task_id or task.get("status") != "ready":
            raise AssertionError(f"Writer fixture received a non-ready source task: {task_id}")
        source_claim_ids = [str(value) for value in task.get("source_claim_ids") or () if str(value)]
        evidence_ids = [str(value) for value in task.get("evidence_ids") or () if str(value)]
        source_field_ids = [str(value) for value in task.get("source_field_ids") or () if str(value)]
        if not source_claim_ids or not evidence_ids:
            raise AssertionError(f"Writer task {task_id} omitted canonical claim/evidence identities")
        # A paper-level claim can be source-closed without a resolved source
        # field.  In particular, an author-stated limitation may have an
        # explicitly unresolved field scope; attaching that field as a
        # qualifier would overstate its authority.  Resolved qualifier
        # closures still require their complete field identity set.
        qualifier_source_field_ids = [
            str(value) for value in task.get("qualifier_source_field_ids") or () if str(value)
        ]
        if qualifier_source_field_ids and not source_field_ids:
            raise AssertionError(
                f"Writer task {task_id} omitted source-field identities for its qualifier closure"
            )
        canonical_rows = [
            dict(item)
            for item in task.get("canonical_source_bundle_refs") or ()
            if isinstance(item, Mapping)
        ]
        task_source_claims = set(source_claim_ids)
        task_evidence = set(evidence_ids)
        task_fields = set(source_field_ids)
        task_qualifier_claims = {
            str(value) for value in task.get("qualifier_source_claim_ids") or () if str(value)
        }
        task_qualifier_evidence = {
            str(value) for value in task.get("qualifier_evidence_ids") or () if str(value)
        }
        task_qualifier_fields = set(qualifier_source_field_ids)
        # The wire projection is content-addressed. Reconstruct dependency
        # rows from its evidence store and require the task's qualifier
        # arrays to contain the exact canonical closure. This preserves valid
        # fieldless paper-level claims while rejecting omitted or unresolved
        # qualifier support.
        bundles = canonical_bundles.get(task_id, [])
        dependencies = [value for row in bundles for value in row["interpretation_dependencies"]]
        required_claims: set[str] = set()
        required_evidence: set[str] = set()
        required_fields: set[str] = set()
        if canonical_rows:
            for collection, identity, expected_ids in (
                ("source_claims", "claim_id", task_source_claims),
                ("evidence", "evidence_id", task_evidence),
                ("source_fields", "source_field_id", task_fields),
            ):
                if {str(value[identity]) for row in bundles for value in row[collection]} != expected_ids:
                    raise AssertionError(f"Writer task {task_id} differs from canonical {collection} identities")
            if any(value.get("scope") == "unresolved" for row in bundles for value in row["source_fields"]):
                raise AssertionError(f"Writer task {task_id} retains an unresolved source field")
            if not task_qualifier_claims.issubset(task_source_claims):
                raise AssertionError(f"Writer task {task_id} has foreign qualifier claim identities")
            if not task_qualifier_evidence.issubset(task_evidence):
                raise AssertionError(f"Writer task {task_id} has foreign qualifier evidence identities")
            if not task_qualifier_fields.issubset(task_fields):
                raise AssertionError(f"Writer task {task_id} has foreign qualifier source-field identities")
            for dependency in dependencies:
                primary_id = str(dependency.get("primary_claim_id") or "")
                if primary_id not in task_source_claims:
                    continue
                if str(dependency.get("scope") or "") not in {"paper", "explicit_study"}:
                    raise AssertionError(f"Writer task {task_id} retains an unresolved interpretation dependency")
                required_claims.update(str(value) for value in dependency.get("required_source_claim_ids") or () if str(value))
                required_evidence.update(str(value) for value in dependency.get("required_evidence_ids") or () if str(value))
                required_fields.update(str(value) for value in dependency.get("required_source_field_ids") or () if str(value))
            if not required_claims.issubset(task_qualifier_claims):
                raise AssertionError(f"Writer task {task_id} omitted canonical qualifier claim identities")
            if not required_evidence.issubset(task_qualifier_evidence):
                raise AssertionError(f"Writer task {task_id} omitted canonical qualifier evidence identities")
            if not required_fields.issubset(task_qualifier_fields):
                raise AssertionError(f"Writer task {task_id} omitted canonical qualifier source-field identities")
        units = [item for item in task.get("output_units") or () if isinstance(item, Mapping)]
        primary_unit = next((item for item in units if item.get("unit_kind") == "planned_claim" and item.get("required") is True), None)
        if not isinstance(primary_unit, Mapping):
            raise TypeError(f"Writer task {task_id} has no required planned-claim output unit")
        allowed_refs = [str(value) for value in task.get("allowed_ref_ids") or () if str(value)]
        if not allowed_refs:
            raise AssertionError(f"Writer task {task_id} has no active citation ref from its source packet")
        planned_claim = str(task.get("planned_claim") or "").strip()
        if not planned_claim:
            raise AssertionError(f"Writer task {task_id} has no source-bound planned claim")
        text = _single_sentence_writer_claim(planned_claim)
        canonical_source_rows = [
            dict(item)
            for item in task.get("canonical_source_bundle_refs") or ()
            if isinstance(item, Mapping)
        ]
        source_papers: list[str] = []
        if not canonical_source_rows and not catalog_entries:
            # Older direct fixture tests construct only a finite allowlist.
            # The production HTTP path always supplies the verified canonical
            # source bundle refs and citation catalog, handled below.
            citation = ", ".join(allowed_refs)
            text = f"{text} [[cite_ref:{citation}]]."
        else:
            if not canonical_source_rows or not catalog_entries:
                raise AssertionError(
                    f"Writer task {task_id} lacks its canonical source rows or citation catalog"
                )
            source_papers = sorted({
                str(row.get("paper_key") or "").strip()
                for row in canonical_source_rows
                if str(row.get("paper_key") or "").strip()
            })
            if set(source_papers) != {
                str(value).strip()
                for value in task.get("paper_keys") or ()
                if str(value).strip()
            }:
                raise AssertionError(
                    f"Writer task {task_id} canonical source rows differ from its paper scope"
                )
            refs_by_paper: dict[str, str] = {}
            for paper_key in source_papers:
                entries = [
                    row for row in catalog_entries
                    if row.get("status") == "active"
                    and str(row.get("canonical_paper_key") or "").strip() == paper_key
                    and str(row.get("ref_id") or "").strip() in set(allowed_refs)
                ]
                if len(entries) != 1:
                    raise AssertionError(
                        f"Writer task {task_id} does not have exactly one allowed catalog ref for {paper_key}"
                    )
                ref_id = str(entries[0].get("ref_id") or "").strip()
                title = str(entries[0].get("title") or "").strip()
                if not title:
                    raise AssertionError(
                        f"Writer task {task_id} citation catalog omits the title for {paper_key}"
                    )
                refs_by_paper[paper_key] = ref_id
            if set(refs_by_paper.values()) != set(allowed_refs):
                raise AssertionError(
                    f"Writer task {task_id} allowed refs differ from its source-paper catalog"
                )

            catalog_by_paper = {
                str(row.get("canonical_paper_key") or "").strip(): row
                for row in catalog_entries
                if str(row.get("ref_id") or "").strip() in set(allowed_refs)
                and str(row.get("canonical_paper_key") or "").strip() in refs_by_paper
            }
            rendered_titles = {
                paper: _single_sentence_writer_claim(str(catalog_by_paper[paper].get("title") or ""))
                for paper in source_papers
            }
            # Equal authored text can legitimately have several independent
            # source supports. Attribute that one shared assertion jointly only
            # when every canonical source supplies the same complete claim set.
            source_statements = [_writer_source_statement_signature(bundles, paper) for paper in source_papers]
            shared_statement = (
                len(source_papers) > 1
                and len({title.casefold() for title in rendered_titles.values()}) == len(source_papers)
                and all(title.casefold() not in text.casefold() for title in rendered_titles.values())
                and bool(source_statements[0])
                and all(statement == source_statements[0] for statement in source_statements)
            )
            if shared_statement:
                attribution = " and ".join(
                    f"{rendered_titles[paper]} [[cite_ref:{refs_by_paper[paper]}]]"
                    for paper in source_papers
                )
                text = f"{attribution} each report {text}"
            located: list[tuple[int, int, str]] = []
            folded_text = text.casefold()
            for paper_key in (() if shared_statement else source_papers):
                title = str(catalog_by_paper[paper_key].get("title") or "").strip()
                # Sentence joining also normalizes punctuation inside a title.
                # Match the same lexical form without changing the catalog.
                rendered_title = _single_sentence_writer_claim(title)
                folded_title = rendered_title.casefold()
                if len(source_papers) == 1 and folded_title not in folded_text:
                    # A canonical bank claim need not repeat its bibliography
                    # label. The model may attribute that unchanged assertion
                    # to the one exact source supplied in this task's catalog.
                    text = f"{rendered_title} reports {text}"
                    folded_text = text.casefold()
                matches: list[int] = []
                cursor = 0
                while folded_title and (offset := folded_text.find(folded_title, cursor)) >= 0:
                    matches.append(offset)
                    cursor = offset + len(folded_title)
                if len(matches) != 1:
                    raise AssertionError(
                        f"Writer task {task_id} cannot locate one source-specific clause for catalog title {title!r}"
                    )
                located.append((matches[0], matches[0] + len(rendered_title), refs_by_paper[paper_key]))
            located.sort(key=lambda row: row[0])
            if len(located) == 1:
                citation = f"[[cite_ref:{located[0][2]}]]"
                text = f"{text.rstrip(' .!?;')} {citation}."
            else:
                citation_anchors: list[tuple[int, str]] = []
                for index, (start, end, ref_id) in enumerate(located):
                    if index + 1 == len(located):
                        anchor = len(text.rstrip(" .!?;"))
                    else:
                        next_start = located[index + 1][0]
                        between = text[end:next_start]
                        transitions = list(re.finditer(
                            r"\b(?:whereas|while|by contrast|in contrast)\b",
                            between,
                            flags=re.IGNORECASE,
                        ))
                        anchor = end + transitions[-1].start() if transitions else next_start
                    citation_anchors.append((anchor, f"[[cite_ref:{ref_id}]]"))
                for anchor, citation in reversed(citation_anchors):
                    left = text[:anchor].rstrip()
                    right = text[anchor:].lstrip()
                    text = f"{left} {citation} {right}".strip()
                text = f"{text.rstrip(' .!?;')}."
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
            "paper_keys": list(source_papers),
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
            "canonical_dependencies": [dict(row) for row in dependencies],
            "required_qualifier_ids": {
                "source_claim_ids": sorted(required_claims),
                "evidence_ids": sorted(required_evidence),
                "source_field_ids": sorted(required_fields),
            },
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


_ORGANIZATION_TRACE_HEADERS = (
    "candidate_id", "axis_hash", "plan_hash", "bank_hash", "map_schema_hash",
    "metadata_catalog_hash", "metadata_catalog_contract_hash",
    "required_group_indices", "required_paper_indices", "organization_contract_hash",
)


def _candidate_fragment_trace(
    node_id: str, request: Mapping[str, Any], response: Mapping[str, Any],
) -> dict[str, Any]:
    """Record compact identities directly from the validated fixture exchange."""
    event: dict[str, Any] = {
        "kind": "candidate_fragment_bank", "node_id": node_id,
        "request_schema_version": request.get("schema_version"),
        "response_schema_version": response.get("schema_version"),
    }
    if response.get("schema_version") == "candidate-comparison-fragments-output/v1":
        headers = ("parent_scope_hash", "scope_hash")
        event.update({
            "branch": "comparison",
            "request_binding": copy.deepcopy({key: request[key] for key in headers}),
            "response_binding": copy.deepcopy({key: response[key] for key in headers}),
            "expected_group_indices": list(range(len(request["comparison_scope"]["claim_groups"]))),
            "materialized_group_indices": [
                row["group_index"] for row in response["fragments"]
                if row["materialization_state"] == "materialized"
            ],
            "fragment_count": len(response["fragments"]),
        })
    elif response.get("schema_version") == "candidate-organization-map/v1":
        event.update({
            "branch": "organization",
            "request_binding": copy.deepcopy({key: request[key] for key in _ORGANIZATION_TRACE_HEADERS}),
            "response_binding": copy.deepcopy({key: response[key] for key in _ORGANIZATION_TRACE_HEADERS}),
            "section_count": len(response["sections"]),
        })
    else:
        raise AssertionError("unknown candidate fixture response schema")
    return event


def _assert_fragment_bank_fixture_trace(fixture_trace: list[dict[str, Any]]) -> None:
    rows = [row for row in fixture_trace if row.get("kind") == "candidate_fragment_bank"]
    assert {row.get("branch") for row in rows} == {"comparison", "organization"}, rows
    for row in rows:
        assert row["node_id"], row
        binding = row["request_binding"]
        assert binding == row["response_binding"], row
        if row["branch"] == "comparison":
            assert row["request_schema_version"] == "candidate-comparison-fragments-request/v1", row
            assert row["response_schema_version"] == "candidate-comparison-fragments-output/v1", row
            assert set(binding) == {"parent_scope_hash", "scope_hash"}, row
            assert all(binding.values()), row
            expected, actual = row["expected_group_indices"], row["materialized_group_indices"]
            assert expected and sorted(actual) == expected, row
            assert len(actual) == row["fragment_count"], row
        else:
            assert row["request_schema_version"] == "candidate-organization-request/v1", row
            assert row["response_schema_version"] == "candidate-organization-map/v1", row
            assert set(binding) == set(_ORGANIZATION_TRACE_HEADERS), row
            assert all(binding[key] for key in _ORGANIZATION_TRACE_HEADERS
                       if key not in {"required_group_indices", "required_paper_indices"}), row
            assert row["section_count"] > 0, row


def _assert_source_bound_fixture_trace(
    fixture_trace: list[dict[str, Any]],
    *,
    require_candidate_source_bound: bool = True,
) -> None:
    topic_rows = [item for item in fixture_trace if item.get("kind") == "topic_source_bound"]
    candidate_rows = [item for item in fixture_trace if item.get("kind") == "candidate_source_bound"]
    writer_rows = [item for item in fixture_trace if item.get("kind") == "writer_source_bound"]
    assert topic_rows, "the production topic request never used the canonical source-bound fixture branch"
    if require_candidate_source_bound:
        assert candidate_rows, "the production candidate request never used the interpretation source tables"
    elif not candidate_rows:
        _assert_fragment_bank_fixture_trace(fixture_trace)
    assert writer_rows, "the production Writer request never used its task scope wire"
    assert any(row["claims"] for row in topic_rows), topic_rows
    assert any(row["claim_closures"] for row in topic_rows), topic_rows
    for row in topic_rows:
        closure_by_claim = {
            str(closure.get("synthesis_claim_id") or ""): closure
            for closure in row["claim_closures"]
            if str(closure.get("synthesis_claim_id") or "")
        }
        assert all(claim["source_claim_ids"] and claim["evidence_ids"] for claim in row["claims"])
        for claim in row["claims"]:
            closure = closure_by_claim[str(claim.get("claim_id") or "")]
            assert set(claim["source_field_ids"]) == set(closure["source_field_ids"]), claim
        for closure in row["claim_closures"]:
            assert closure["source_claim_ids"] and closure["evidence_ids"], closure
            assert set(closure["qualifier_source_claim_ids"]).issubset(closure["source_claim_ids"]), closure
            assert set(closure["qualifier_evidence_ids"]).issubset(closure["evidence_ids"]), closure
            assert set(closure["source_field_ids"]) == set(closure["qualifier_source_field_ids"]), closure
            dependencies = closure["dependency_rows"]
            for qualifier_field, required_field in (
                ("qualifier_source_claim_ids", "required_source_claim_ids"),
                ("qualifier_evidence_ids", "required_evidence_ids"),
                ("qualifier_source_field_ids", "required_source_field_ids"),
            ):
                assert set(closure[qualifier_field]) == {
                    str(value) for dependency in dependencies
                    for value in dependency.get(required_field) or ()
                }, closure
            if dependencies:
                assert (
                    closure["qualifier_source_claim_ids"]
                    or closure["qualifier_evidence_ids"]
                    or closure["qualifier_source_field_ids"]
                ), closure
            else:
                assert not closure["qualifier_source_claim_ids"], closure
                assert not closure["qualifier_evidence_ids"], closure
                assert not closure["qualifier_source_field_ids"], closure
        assert row["topics"]
        for topic in row["topics"]:
            if topic["status"] == "completed":
                fragment_id = str(topic.get("fragment_id") or "")
                matching_closures = [
                    closure for closure in row["claim_closures"]
                    if str(closure.get("fragment_id") or "") == fragment_id
                ]
                assert topic["source_claim_ids"] and topic["evidence_ids"], topic
                assert set(topic["source_field_ids"]) == {
                    str(value)
                    for closure in matching_closures
                    for value in closure.get("source_field_ids") or ()
                }, topic
            else:
                assert topic["status"] == "unresolved", topic
                assert not topic["source_claim_ids"] and not topic["evidence_ids"] and not topic["source_field_ids"], topic
                assert topic["unresolved_questions"], topic
    for row in candidate_rows:
        assert row["sections"]
        for section in row["sections"]:
            assert section["source_claim_ids"] and section["evidence_ids"], section
            assert set(section["qualifier_source_claim_ids"]).issubset(section["source_claim_ids"]), section
            assert set(section["qualifier_evidence_ids"]).issubset(section["evidence_ids"]), section
            assert set(section["qualifier_source_field_ids"]).issubset(section["source_field_ids"]), section
            if section["dependency_ids"]:
                assert (
                    section["qualifier_source_claim_ids"]
                    or section["qualifier_evidence_ids"]
                    or section["qualifier_source_field_ids"]
                ), section
            else:
                assert not section["qualifier_source_claim_ids"], section
                assert not section["qualifier_evidence_ids"], section
                assert not section["qualifier_source_field_ids"], section
    for row in writer_rows:
        assert row["schema_version"] in {"writer_task_scope_wire/v1", "writer_task_scope_wire/v2"}
        assert row["writer_task_basis_hash"]
        required_kinds = {"source_claim", "evidence"}
        if any(task["source_field_ids"] for task in row["tasks"]):
            required_kinds.add("source_field")
        assert required_kinds.issubset(set(row["source_store_kinds"]))
        assert len(row["returned_output_unit_ids"]) == row["task_count"]
        assert all(item["disposition"] == "covered" for item in row["dispositions"])
        assert len(row["tasks"]) == row["task_count"]
        assert all(
            task["source_claim_ids"]
            and task["evidence_ids"]
            and task["planned_claim"]
            and task["allowed_ref_ids"]
            and task["required_output_unit_id"]
            and (
                all(set(task["required_qualifier_ids"][field]).issubset(task[f"qualifier_{field}"])
                    for field in ("source_claim_ids", "evidence_ids", "source_field_ids"))
            )
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
    arbitration_fixture_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from outline.semantic_request_wire import expand_semantic_request_wire
    from tests.arbitration_overflow_model_fixture import build_arbitration_overflow_response
    from tests.run_first_candidate_fragments_fixture import candidate_fragment_fixture_response

    if isinstance(request.get("arbitration_assessment_wire"), Mapping):
        overflow_response = build_arbitration_overflow_response(
            node_id, request, trace=fixture_trace, state=arbitration_fixture_state,
        )
        if overflow_response is None:
            raise AssertionError("model fixture did not recognize assessment wire schema")
        return _provider_response(overflow_response)
    request = expand_semantic_request_wire(request)
    overflow_response = build_arbitration_overflow_response(
        node_id, request, trace=fixture_trace, state=arbitration_fixture_state,
    )
    if overflow_response is not None:
        return _provider_response(overflow_response)
    fragment_response = candidate_fragment_fixture_response(node_id, request)
    if fragment_response is not None:
        if fixture_trace is not None:
            fixture_trace.append(_candidate_fragment_trace(node_id, request, fragment_response))
        return _provider_response(fragment_response)
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
        if fixture_trace is not None:
            topic_members = _cross_group_fixture_topic_memberships(request)
            if topic_members:
                fixture_trace.append({
                    "kind": "outline_topic_membership",
                    "node_id": node_id,
                    "topic_members_by_id": {
                        key: sorted(value) for key, value in sorted(topic_members.items())
                    },
                })
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

    if node_id.startswith("cross_group_comparison_provider"):
        content = _source_closed_cross_group_content(
            request, node_id=node_id, fixture_trace=fixture_trace,
        )
        return _provider_response(_fixture_semantic_output_for_contract(request, content))
    if node_id.startswith("global_synthesis_provider"):
        content = _source_closed_global_content(request)
        return _provider_response(_fixture_semantic_output_for_contract(request, content))

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

    if request.get("schema_version") == "outline_candidate_arbitration_leaf/v1":
        from outline.candidate_arbitration_packing import expand_arbitration_candidate_support

        expanded = expand_arbitration_candidate_support(request)
        part_bindings = expanded.get("claim_support_part_bindings")
        rows = []
        for candidate_id in expanded["candidate_ids"]:
            sections = expanded["candidate_contents"][candidate_id]["sections"]
            bindings = expanded["candidate_section_bindings"][candidate_id]
            issues = expanded["candidate_critique_issues"][candidate_id]
            refs = sorted({
                str(value) for section in sections for support in section.get("claim_support") or ()
                for key in ("evidence_ids", "source_claim_ids", "source_field_ids")
                for value in support.get(key) or () if str(value)
            })
            row = {
                "candidate_id": candidate_id,
                "candidate_hash": expanded["candidate_hashes"][candidate_id],
                "presence": "present" if sections else "absent",
                "reviewed_section_ids": sorted(row["section_id"] for row in bindings),
                "section_hashes": {row["section_id"]: row["section_hash"] for row in bindings},
                "coverage_assessment": (
                    (
                        "The assigned source-closed section/claim-support views retain their bound task coverage."
                        if isinstance(part_bindings, Mapping)
                        else f"The assigned {len(sections)} complete sections retain their source task coverage."
                    )
                    if sections else "No source section is assigned to this candidate in this leaf."
                ),
                "evidence_assessment": (
                    "All assigned authored claims carry their full source support and qualifier identities."
                    if sections else "No evidence assessment is made without an assigned section."
                ),
                "structure_assessment": (
                    "Compare each section's distinct task, source role and position in the full section index."
                    if sections else "No local structure assessment is made without an assigned section."
                ),
                "advantages": ["Retains source boundaries and complete comparison support."] if sections else [],
                "limitations": ["The local evaluation does not establish broader source generalizability."] if sections else [],
                "evidence_refs": refs[:1] if sections else [],
                "critic_issue_ids": sorted(str(issue["issue_id"]) for issue in issues) if sections else [],
            }
            if isinstance(part_bindings, Mapping):
                row["claim_support_part_receipts"] = [
                    {
                        "part_id": part["part_id"],
                        "part_hash": part["part_hash"],
                        "parent_section_bindings": [
                            {"section_id": binding["section_id"], "section_hash": binding["section_hash"]}
                            for binding in part["parent_bindings_by_candidate"][candidate_id]
                        ],
                    }
                    for part in part_bindings.get("parts") or ()
                ]
            rows.append(row)
        return _provider_response({
            "leaf_id": expanded["hierarchy"]["leaf_id"],
            "unit_ids": expanded["hierarchy"]["unit_ids"],
            "candidate_assessments": rows,
        })
    if node_id == "arbitration" or request.get("hierarchy", {}).get("level") == "all_candidate_final_arbitration":
        from outline.critique_coverage_wire import expand_critique_coverage_certificates

        request = expand_critique_coverage_certificates(request)
        if request.get("arbitration_final_projection"):
            from outline.candidate_arbitration_final_projection import expand_arbitration_final_projection

            request = expand_arbitration_final_projection(request)
        if (request.get("arbitration_evaluation_ledger") or {}).get("ledger_packing"):
            from outline.candidate_arbitration_ledger_packing import expand_arbitration_final_ledger

            request = {**dict(request), "arbitration_evaluation_ledger": expand_arbitration_final_ledger(
                request["arbitration_evaluation_ledger"],
            )}
        candidate_ids = [str(item) for item in request.get("candidate_ids") or ()]
        selected = sorted(candidate_ids)[0] if candidate_ids else ""
        content: dict[str, Any] = {
            "selected_candidate_id": selected,
            "selection_reasons": ["The selected alternative retains complete verified source coverage and section order."],
            "accepted_recommendations": [],
            "rejected_recommendations": [],
        }
        contract = request.get("section_coordination_contract") or {}
        if selected in (contract.get("required_if_selected_candidate_sharded") or ()):
            candidate = (request.get("candidate_contents") or {}).get(selected) or {}
            selected_sections = candidate.get("sections") or (
                (request.get("arbitration_evaluation_ledger") or {}).get("candidate_section_index") or {}
            ).get(selected, [])
            content["section_coordination"] = {
                "candidate_id": selected,
                "merge_groups": [],
                "section_order": [
                    str(section.get("section_id") or "")
                    for section in selected_sections
                    if isinstance(section, Mapping)
                ],
            }
        return _provider_response(content)
    return _provider_response({"node_id": node_id, "accepted": True})


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _cross_group_fixture_topic_memberships(
    request: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]] | None = None,
) -> dict[str, set[str]]:
    """Project only explicit topic-to-paper ownership for the local fixture."""

    members_by_topic: dict[str, set[str]] = {}

    def add(topic_id: Any, paper_ids: Any) -> None:
        key = str(topic_id or "").strip()
        if not key:
            return
        values = {
            str(item).strip()
            for item in _as_list(paper_ids)
            if isinstance(item, str) and item.strip()
        }
        if values:
            members_by_topic.setdefault(key, set()).update(values)

    def visit(value: Any, inherited_topic_id: str = "") -> None:
        if isinstance(value, Mapping):
            explicit_members = value.get("topic_members_by_id")
            if isinstance(explicit_members, Mapping):
                for topic_id, paper_ids in explicit_members.items():
                    add(topic_id, paper_ids)
            topic_id = str(value.get("topic_id") or inherited_topic_id).strip()
            if topic_id and isinstance(value.get("paper_ids"), list):
                add(topic_id, [
                    *list(value.get("paper_ids") or ()),
                    *list(value.get("bridge_paper_ids") or ()),
                ])
            # Do not distribute a group-level paper union over plural topic IDs.
            # Per-topic membership must come from a singular route or an
            # explicit topic_members_by_id map.
            for key, child in value.items():
                if key in {"claims", "bridge_claims", "synthesis_claims", "provider_output", "semantic_result"}:
                    continue
                visit(child, topic_id)
        elif isinstance(value, list):
            for child in value:
                visit(child, inherited_topic_id)

    for key in ("topics", "topic_synthesis", "topic_routes", "hierarchy"):
        visit(request.get(key))
    if fixture_trace is not None:
        for row in fixture_trace:
            if not isinstance(row, Mapping):
                continue
            if row.get("kind") == "outline_topic_membership":
                visit({"topic_members_by_id": row.get("topic_members_by_id") or {}})
            elif row.get("kind") == "topic_source_bound":
                visit({"topic_members_by_id": row.get("topic_members_by_id") or {}})
                paper_by_claim: dict[str, str] = {}
                for claim in row.get("claims") or ():
                    if not isinstance(claim, Mapping):
                        continue
                    paper_key = str(claim.get("paper_key") or "")
                    for claim_id in claim.get("source_claim_ids") or ():
                        if paper_key and str(claim_id):
                            paper_by_claim[str(claim_id)] = paper_key
                for topic in row.get("topics") or ():
                    if not isinstance(topic, Mapping):
                        continue
                    add(
                        topic.get("topic_id"),
                        [paper_by_claim[str(claim_id)] for claim_id in topic.get("source_claim_ids") or ()
                         if str(claim_id) in paper_by_claim],
                    )
    return members_by_topic


def _compact_source_closed_bridge_text(
    claim: Mapping[str, Any],
    *,
    source_claims_by_id: Mapping[str, Mapping[str, Any]],
    source_fields_by_id: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Deduplicate source prose only when every original support row resolves."""

    paper_keys = {
        str(item)
        for key in ("paper_key", "paper_keys")
        for item in _as_list(claim.get(key))
        if str(item)
    }
    source_claim_ids = sorted({
        str(item) for item in claim.get("source_claim_ids") or () if str(item)
    })
    source_field_ids = sorted({
        str(item) for item in claim.get("source_field_ids") or () if str(item)
    })
    if not paper_keys or not source_claim_ids or not source_field_ids:
        return None

    source_rows: list[Mapping[str, Any]] = []
    for source_claim_id in source_claim_ids:
        row = source_claims_by_id.get(source_claim_id)
        if (
            not isinstance(row, Mapping)
            or str(row.get("paper_key") or "") not in paper_keys
            or not str(row.get("text") or "").strip()
        ):
            return None
        source_rows.append(row)

    field_rows: list[Mapping[str, Any]] = []
    for source_field_id in source_field_ids:
        row = source_fields_by_id.get(source_field_id)
        if (
            not isinstance(row, Mapping)
            or str(row.get("paper_key") or "") not in paper_keys
            or not str(row.get("source_value") or "").strip()
        ):
            return None
        field_rows.append(row)

    source_rows.sort(key=lambda row: (
        str(row.get("claim_type") or "") != "empirical_finding",
        str(row.get("source_locator") or ""),
        str(row.get("claim_id") or ""),
    ))
    pieces: list[str] = []
    seen_text: set[str] = set()
    for row in source_rows:
        raw_text = str(row.get("text") or "").strip()
        text = re.sub(r"[\s.!?。！？；;]+$", "", raw_text).strip()
        normalized = " ".join(text.split()).casefold()
        if text and normalized not in seen_text:
            pieces.append(text)
            seen_text.add(normalized)

    def comparable_text(value: str) -> str:
        without_terminator = re.sub(r"[\s.!?。！？；;]+$", "", value).strip()
        return " ".join(without_terminator.split()).casefold()

    rendered = "; ".join(pieces)
    for row in field_rows:
        value = str(row.get("source_value") or "").strip()
        if comparable_text(value) in comparable_text(rendered):
            continue
        label = str(row.get("canonical_field") or row.get("source_path") or "source boundary")
        field_text = re.sub(r"[\s.!?。！？；;]+$", "", value).strip()
        pieces.append(f"{label.replace('_', ' ')}: {field_text}")
        rendered = "; ".join(pieces)
    compact = rendered.rstrip(" ;") + "."
    if any(
        comparable_text(str(row.get("source_value") or "").strip())
        not in comparable_text(compact)
        for row in field_rows
    ):
        return None
    return compact


def _source_closed_cross_group_content(
    request: Mapping[str, Any],
    *,
    node_id: str = "cross_group_comparison_provider",
    fixture_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build fixture bridge claims only from exact, source-closed topic claims."""

    supported: list[tuple[set[str], str, dict[str, Any]]] = []
    requested_topics: set[str] = set()
    requested_fragments: set[str] = set()
    requested_results: set[str] = set()
    requested_relations: set[str] = set()
    visible_fields_by_paper: dict[str, set[str]] = {}
    visible_field_rows_by_id: dict[str, dict[str, Any]] = {}
    rejected_claim_diagnostics: list[dict[str, Any]] = []
    topic_members_by_id = _cross_group_fixture_topic_memberships(request, fixture_trace)
    for topic_fragment in request.get("topic_synthesis") or ():
        if not isinstance(topic_fragment, Mapping):
            continue
        context = topic_fragment.get("interpretation_context")
        if not isinstance(context, Mapping):
            continue
        for raw_field in context.get("fields") or ():
            if not isinstance(raw_field, Mapping):
                continue
            field_id = str(raw_field.get("source_field_id") or "")
            paper_key = str(raw_field.get("paper_key") or "")
            if field_id and paper_key:
                visible_fields_by_paper.setdefault(paper_key, set()).add(field_id)
                candidate_field = dict(raw_field)
                existing_field = visible_field_rows_by_id.get(field_id)
                if existing_field is not None and existing_field != candidate_field:
                    raise AssertionError(
                        f"source field {field_id!r} has conflicting visible content in the reducer request"
                    )
                visible_field_rows_by_id[field_id] = candidate_field

    source_claims_by_id: dict[str, dict[str, Any]] = {}
    conflicting_source_claim_ids: set[str] = set()
    for trace_row in fixture_trace or ():
        if not isinstance(trace_row, Mapping) or trace_row.get("kind") != "topic_source_bound":
            continue
        for closure in trace_row.get("claim_closures") or ():
            if not isinstance(closure, Mapping):
                continue
            for source_claim in closure.get("source_claim_records") or ():
                if not isinstance(source_claim, Mapping):
                    continue
                claim_id = str(source_claim.get("claim_id") or "")
                if not claim_id:
                    continue
                row = dict(source_claim)
                existing = source_claims_by_id.get(claim_id)
                if existing is not None and existing != row:
                    conflicting_source_claim_ids.add(claim_id)
                    source_claims_by_id.pop(claim_id, None)
                    continue
                if claim_id not in conflicting_source_claim_ids:
                    source_claims_by_id[claim_id] = row

    def collect_ids(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in ("topic_id", "topic_ids", "processed_topic_ids"):
                requested_topics.update(
                    str(item) for item in _as_list(value.get(key)) if str(item)
                )
            for key in ("fragment_id", "fragment_ids", "processed_fragment_ids"):
                requested_fragments.update(
                    str(item) for item in _as_list(value.get(key)) if str(item)
                )
            for key in ("result_id", "result_ids", "batch_result_id", "batch_result_ids", "processed_result_ids"):
                requested_results.update(
                    str(item) for item in _as_list(value.get(key)) if str(item)
                )
            for key in ("relation_id", "relation_ids", "processed_relation_ids"):
                requested_relations.update(
                    str(item) for item in _as_list(value.get(key)) if str(item)
                )
            for child in value.values():
                collect_ids(child)
        elif isinstance(value, list):
            for child in value:
                collect_ids(child)

    def collect_claims(
        value: Any,
        inherited_topics: set[str] | None = None,
        inherited_fragment: str = "",
    ) -> None:
        inherited_topics = inherited_topics or set()
        if isinstance(value, Mapping):
            local_topics = set(inherited_topics)
            local_topics.update(
                str(item) for key in ("topic_id", "topic_ids")
                for item in _as_list(value.get(key)) if str(item)
            )
            local_fragment = str(value.get("fragment_id") or inherited_fragment)
            for field in ("claims", "bridge_claims", "synthesis_claims"):
                rows = value.get(field)
                if not isinstance(rows, list):
                    continue
                for raw_claim in rows:
                    if not isinstance(raw_claim, Mapping):
                        continue
                    claim = dict(raw_claim)
                    explicit_claim_topics = {
                        str(item) for key in ("topic_id", "topic_ids")
                        for item in _as_list(claim.get(key)) if str(item)
                    }
                    paper_keys = {
                        str(item)
                        for key in ("paper_key", "paper_keys")
                        for item in _as_list(claim.get(key)) if str(item)
                    }
                    candidate_topics = explicit_claim_topics or set(local_topics)
                    candidate_topics.intersection_update(requested_topics)
                    valid_topics = {
                        topic_id for topic_id in candidate_topics
                        if set(paper_keys).intersection(topic_members_by_id.get(topic_id, set()))
                    }
                    # Compact reducer references do not expose their sealed
                    # topic-to-paper membership to this HTTP fixture. Trusting
                    # their topic IDs here could emit a claim that the later
                    # Registry-backed validator correctly rejects. Exact
                    # ownership must arrive in this request or the explicit
                    # topic-provider trace above.
                    covered_papers = set().union(
                        *(topic_members_by_id.get(topic_id, set()) for topic_id in valid_topics)
                    ) if valid_topics else set()
                    if not paper_keys or not valid_topics or not paper_keys.issubset(covered_papers):
                        rejected_claim_diagnostics.append({
                            "claim_id": str(claim.get("claim_id") or ""),
                            "paper_keys": sorted(paper_keys),
                            "candidate_topic_ids": sorted(candidate_topics),
                            "valid_topic_ids": sorted(valid_topics),
                            "reason": "source paper is not a member of the claim's declared topic set",
                        })
                        continue
                    source_field_ids = {
                        str(item) for item in claim.get("source_field_ids") or () if str(item)
                    }
                    visible_for_claim = set().union(
                        *(visible_fields_by_paper.get(paper, set()) for paper in paper_keys)
                    ) if paper_keys else set()
                    has_source_closure = bool(
                        paper_keys
                        and claim.get("source_claim_ids")
                        and claim.get("evidence_ids")
                        and source_field_ids
                        and str(claim.get("text") or "").strip()
                    )
                    if has_source_closure and not source_field_ids.issubset(visible_for_claim):
                        rejected_claim_diagnostics.append({
                            "claim_id": str(claim.get("claim_id") or ""),
                            "paper_keys": sorted(paper_keys),
                            "source_claim_count": len(claim.get("source_claim_ids") or ()),
                            "evidence_count": len(claim.get("evidence_ids") or ()),
                            "source_field_count": len(source_field_ids),
                            "visible_field_count": len(visible_for_claim),
                        })
                    if (
                        has_source_closure
                        and source_field_ids.issubset(visible_for_claim)
                    ):
                        accepted_claim = dict(claim)
                        accepted_claim.pop("topic_id", None)
                        accepted_claim["topic_ids"] = sorted(valid_topics)
                        supported.append((
                            valid_topics,
                            local_fragment or str(claim.get("fragment_id") or ""),
                            accepted_claim,
                        ))
            for key, child in value.items():
                if key in {"claims", "bridge_claims", "synthesis_claims"}:
                    continue
                collect_claims(child, local_topics, local_fragment)
        elif isinstance(value, list):
            for child in value:
                collect_claims(child, inherited_topics, inherited_fragment)

    for key in ("topic_synthesis", "cross_group_comparison"):
        collect_ids(request.get(key))
    for key in ("topic_synthesis", "cross_group_comparison"):
        collect_claims(request.get(key))
    is_reducer = ":reduce:" in node_id
    if fixture_trace is not None:
        fixture_trace.append({
            "kind": "cross_group_source_membership",
            "node_id": node_id,
            "topic_members_by_id": {
                key: sorted(value) for key, value in sorted(topic_members_by_id.items())
            },
            "accepted_claim_memberships": [
                {
                    "claim_id": str(claim.get("claim_id") or ""),
                    "paper_keys": sorted({
                        str(item)
                        for key in ("paper_key", "paper_keys")
                        for item in _as_list(claim.get(key)) if str(item)
                    }),
                    "topic_ids": sorted(topic_ids),
                }
                for topic_ids, _fragment, claim in supported
            ],
            "rejected_claim_diagnostics": rejected_claim_diagnostics,
        })
    if not requested_topics and not is_reducer:
        raise AssertionError("cross-group request has no topic identities")
    if not supported:
        if is_reducer:
            return {
                "comparisons": [],
                "bridge_claims": [],
                "topic_dispositions": [
                    {
                        "topic_id": topic_id,
                        "status": "deferred",
                        "reason": "This reducer slice has no complete source-closed comparison claim.",
                    }
                    for topic_id in sorted(requested_topics)
                ],
                "processed_topic_ids": sorted(requested_topics),
                "processed_fragment_ids": sorted(requested_fragments),
                "processed_result_ids": sorted(requested_results),
                "processed_relation_ids": sorted(requested_relations),
                "unresolved_questions": [],
            }
        raise AssertionError(
            "cross-group request has no source-closed claim to integrate; "
            f"visible field counts={{{', '.join(f'{key}:{len(value)}' for key, value in sorted(visible_fields_by_paper.items()))}}}; "
            f"rejected claims={rejected_claim_diagnostics[:6]}"
        )

    relation_rows = [
        row for row in request.get("relation_candidates") or ()
        if isinstance(row, Mapping)
    ]
    relation_rows.sort(key=lambda row: str(row.get("relation_id") or ""))
    paired: tuple[
        tuple[set[str], str, dict[str, Any]],
        tuple[set[str], str, dict[str, Any]],
        Mapping[str, Any] | None,
    ] | None = None
    for relation in relation_rows:
        relation_papers = {
            str(item) for item in relation.get("paper_keys") or () if str(item)
        }
        if len(relation_papers) != 2:
            continue
        sides = [
            next(
                (
                    row for row in supported
                    if set(row[2].get("paper_keys") or ([row[2].get("paper_key")] if row[2].get("paper_key") else ())) == {paper}
                ),
                None,
            )
            for paper in sorted(relation_papers)
        ]
        if all(sides):
            paired = (sides[0], sides[1], relation)
            break

    prior_bridge_claims_by_id: dict[str, dict[str, Any]] = {}
    for claim_topics, _fragment, claim in supported:
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id.startswith("synthesis:cross_group_comparison:"):
            continue
        preserved = dict(claim)
        preserved["topic_ids"] = sorted(claim_topics)
        compact_text = _compact_source_closed_bridge_text(
            preserved,
            source_claims_by_id=source_claims_by_id,
            source_fields_by_id=visible_field_rows_by_id,
        )
        if compact_text:
            preserved["text"] = compact_text
        prior_bridge_claims_by_id.setdefault(claim_id, preserved)

    def comparison_from_bridge_claim(claim: Mapping[str, Any]) -> dict[str, Any] | None:
        paper_keys = sorted({
            str(item)
            for key in ("paper_key", "paper_keys")
            for item in _as_list(claim.get(key)) if str(item)
        })
        if len(paper_keys) < 2:
            return None
        row = {
            "text": str(claim.get("text") or "").strip(),
            "topic_ids": sorted({str(item) for item in claim.get("topic_ids") or () if str(item)}),
            "paper_keys": paper_keys,
            "source_claim_ids": sorted({str(item) for item in claim.get("source_claim_ids") or () if str(item)}),
            "source_field_ids": sorted({str(item) for item in claim.get("source_field_ids") or () if str(item)}),
            "evidence_ids": sorted({str(item) for item in claim.get("evidence_ids") or () if str(item)}),
        }
        relation_ids = sorted({str(item) for item in claim.get("relation_ids") or () if str(item)})
        if relation_ids:
            row["relation_ids"] = relation_ids
        return row

    if paired is None and prior_bridge_claims_by_id:
        # A final fold with no fresh relation input should preserve each
        # source-closed child bridge as-is. Re-synthesizing the first supported
        # source row here used to widen its topic_ids to its parent reducer's
        # unrelated topic set and could silently discard sibling bridges.
        preserved_bridges = [
            prior_bridge_claims_by_id[key]
            for key in sorted(prior_bridge_claims_by_id)
        ]
        integrated_by_topic: dict[str, list[str]] = {}
        for claim in preserved_bridges:
            claim_id = str(claim.get("claim_id") or "")
            for topic_id in claim.get("topic_ids") or ():
                if str(topic_id):
                    integrated_by_topic.setdefault(str(topic_id), []).append(claim_id)
        return {
            "comparisons": [
                row for row in (
                    comparison_from_bridge_claim(claim) for claim in preserved_bridges
                ) if row is not None
            ],
            "bridge_claims": preserved_bridges,
            "topic_dispositions": [
                {
                    "topic_id": topic_id,
                    "status": "integrated" if topic_id in integrated_by_topic else "deferred",
                    **(
                        {"synthesis_claim_ids": sorted(set(integrated_by_topic[topic_id]))}
                        if topic_id in integrated_by_topic
                        else {"reason": "No validated source-closed bridge claim covers this topic in the bounded fold."}
                    ),
                }
                for topic_id in sorted(requested_topics)
            ],
            "processed_topic_ids": sorted(requested_topics),
            "processed_fragment_ids": sorted(requested_fragments),
            "processed_result_ids": sorted(requested_results),
            "processed_relation_ids": sorted(requested_relations),
            "unresolved_questions": [],
        }
    chosen = [paired[0], paired[1]] if paired is not None else [supported[0]]
    relation = paired[2] if paired is not None else None
    paper_keys = sorted({
        str(item)
        for _topics, _fragment, claim in chosen
        for key in ("paper_key", "paper_keys")
        for item in _as_list(claim.get(key)) if str(item)
    })
    topic_ids = sorted({
        topic_id for claim_topics, _fragment, _claim in chosen
        for topic_id in claim_topics if topic_id in requested_topics
    })
    if not topic_ids:
        raise AssertionError("source-closed bridge claim is not linked to a requested topic")
    source_claim_ids = sorted({
        str(item) for _topics, _fragment, claim in chosen
        for item in claim.get("source_claim_ids") or () if str(item)
    })
    evidence_ids = sorted({
        str(item) for _topics, _fragment, claim in chosen
        for item in claim.get("evidence_ids") or () if str(item)
    })
    source_field_ids = sorted({
        str(item) for _topics, _fragment, claim in chosen
        for item in claim.get("source_field_ids") or () if str(item)
    })
    if paired is None:
        bridge_text = str(chosen[0][2].get("text") or "").strip()
    else:
        bridge_text = (
            "Compared side by side, the included source records report: "
            + "; whereas ".join(
                str(claim.get("text") or "").strip().rstrip(".。;； ")
                for _topics, _fragment, claim in chosen
            )
            + ". Each statement retains its own source boundary."
        )
    bridge_id = "synthesis:cross_group_comparison:fixture-" + hashlib.sha256(
        ("|".join(topic_ids) + "|" + "|".join(source_claim_ids)).encode("utf-8")
    ).hexdigest()[:16]
    bridge_claim: dict[str, Any] = {
        "claim_id": bridge_id,
        "topic_ids": topic_ids,
        "paper_keys": paper_keys,
        "text": bridge_text,
        "source_claim_ids": source_claim_ids,
        "source_field_ids": source_field_ids,
        "evidence_ids": evidence_ids,
    }
    relation_id = str(relation.get("relation_id") or "") if isinstance(relation, Mapping) else ""
    if relation_id:
        bridge_claim["relation_ids"] = [relation_id]
    study_ids = {
        str(claim.get("study_id") or "") for _topics, _fragment, claim in chosen
        if str(claim.get("study_id") or "")
    }
    if len(study_ids) == 1:
        bridge_claim["study_id"] = next(iter(study_ids))
    comparison = {
        "text": bridge_text,
        "topic_ids": topic_ids,
        "paper_keys": paper_keys,
        "source_claim_ids": source_claim_ids,
        "source_field_ids": source_field_ids,
        "evidence_ids": evidence_ids,
    }
    if relation_id:
        comparison["relation_ids"] = [relation_id]
    selected_claim_ids = {
        str(claim.get("claim_id") or "") for _topics, _fragment, claim in chosen
    }
    preserved_prior = [
        claim
        for claim_id, claim in sorted(prior_bridge_claims_by_id.items())
        if claim_id not in selected_claim_ids and claim_id != bridge_id
    ]
    bridge_claims = [bridge_claim, *preserved_prior]
    bridge_claims_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in bridge_claims if str(claim.get("claim_id") or "")
    }
    bridge_claims = [bridge_claims_by_id[key] for key in sorted(bridge_claims_by_id)]
    comparisons = [comparison] if paired is not None else []
    comparisons.extend(
        row for row in (
            comparison_from_bridge_claim(claim) for claim in preserved_prior
        ) if row is not None
    )
    integrated_claim_ids_by_topic: dict[str, set[str]] = {}
    for claim in bridge_claims:
        claim_id = str(claim.get("claim_id") or "")
        for topic_id in claim.get("topic_ids") or ():
            if str(topic_id):
                integrated_claim_ids_by_topic.setdefault(str(topic_id), set()).add(claim_id)
    integrated_topics = set(integrated_claim_ids_by_topic)
    return {
        "comparisons": comparisons,
        "bridge_claims": bridge_claims,
        "topic_dispositions": [
            {
                "topic_id": topic_id,
                "status": "integrated" if topic_id in integrated_topics else "deferred",
                **(
                    {"synthesis_claim_ids": sorted(integrated_claim_ids_by_topic[topic_id])}
                    if topic_id in integrated_topics
                    else {"reason": "No selected source-closed bridge claim covers this requested topic in the bounded fixture."}
                ),
            }
            for topic_id in sorted(requested_topics)
        ],
        "processed_topic_ids": sorted(requested_topics),
        "processed_fragment_ids": sorted(requested_fragments),
        "processed_result_ids": sorted(requested_results),
        "processed_relation_ids": sorted(requested_relations),
        "unresolved_questions": [],
    }


def _source_closed_global_content(request: Mapping[str, Any]) -> dict[str, Any]:
    cross = request.get("cross_group_comparison")
    bridges = cross.get("bridge_claims") if isinstance(cross, Mapping) else None
    scoped = "global_retention_source" in request and not cross
    unresolved: list[str] = []
    if scoped:
        bridges = []

        def source_rows(value: Any) -> None:
            if isinstance(value, Mapping):
                for key in ("bridge_claims", "synthesis_claims"):
                    for row in value.get(key) or []:
                        if isinstance(row, Mapping):
                            bridges.append(row)
                for reason in value.get("unresolved_questions") or []:
                    if isinstance(reason, str) and reason not in unresolved:
                        unresolved.append(reason)
                for row in value.get("topic_disposition_groups") or []:
                    if isinstance(row, Mapping) and row.get("status") != "integrated" and row.get("reason"):
                        reason = str(row["reason"])
                        if reason not in unresolved:
                            unresolved.append(reason)
                for key, child in value.items():
                    if key not in {"bridge_claims", "synthesis_claims"}:
                        source_rows(child)
            elif isinstance(value, list):
                for child in value:
                    source_rows(child)

        source_rows(request.get("topic_synthesis"))
        if not bridges and (request.get("hierarchy") or {}).get("level") == "bounded_semantic_reduction":
            if not unresolved:
                raise AssertionError("deferred GLOBAL fixture has no source unresolved reason")
            return {"synthesis_claims": [], "organizing_principles": [], "unresolved_questions": unresolved}
    if not isinstance(bridges, list) or not bridges or not isinstance(bridges[0], Mapping):
        raise AssertionError("global fixture has no validated source-closed bridge")
    bridge = dict(bridges[0])
    topic_ids = sorted({str(value) for value in bridge.get("topic_ids") or () if str(value)})
    papers = sorted({
        str(value) for key in ("paper_key", "paper_keys")
        for value in _as_list(bridge.get(key)) if str(value)
    })
    claim_identity = str(bridge.get("claim_id") or "")
    if scoped:
        # Identifier aliases are local to each request. A raw alias may repeat
        # in another child, so generated fixture claims also bind that scope.
        claim_identity = json.dumps({
            "bridge": claim_identity, "hierarchy": request.get("hierarchy"),
            "source_request_hash": (request.get("global_retention_source") or {}).get("source_request_hash"),
        }, ensure_ascii=False, sort_keys=True)
    claim: dict[str, Any] = {
        "claim_id": "synthesis:global_synthesis:fixture-" + hashlib.sha256(
            claim_identity.encode("utf-8")
        ).hexdigest()[:16],
        "topic_ids": topic_ids,
        "paper_keys": papers,
        "text": str(bridge.get("text") or ""),
        "source_claim_ids": [str(bridge.get("claim_id") or "")],
        # This fold reuses the validated cross-group claim as prior lineage.
        # Its exact field text/IDs are not directly projected into the global
        # request, so keep the cross-claim reference instead of echoing IDs
        # that are outside the global provider's visible source context.
        "evidence_ids": list(bridge.get("evidence_ids") or ()),
    }
    if scoped:
        claim["source_claim_ids"] = sorted({str(bridge.get("claim_id") or ""), *map(str, bridge.get("source_claim_ids") or [])} - {""})
        if bridge.get("source_field_ids"):
            claim["source_field_ids"] = list(bridge["source_field_ids"])
    if bridge.get("study_id"):
        claim["study_id"] = str(bridge["study_id"])
    processed_topics = sorted({
        str(item.get("topic_id") or "") for item in cross.get("topic_dispositions") or ()
        if isinstance(item, Mapping) and str(item.get("topic_id") or "")
    }) if not scoped else sorted(_semantic_identity_sets(request)[0])
    _all_topics, fragments, results, relations = _semantic_identity_sets(request)
    return {
        "synthesis_claims": [claim],
        "organizing_principles": ["Keep source-bound findings together and retain their recorded boundaries."],
        "processed_topic_ids": processed_topics,
        "processed_fragment_ids": sorted(fragments),
        "processed_result_ids": sorted(results),
        "processed_relation_ids": sorted(relations),
        "unresolved_questions": unresolved if scoped else [],
    }


def _semantic_identity_sets(request: Mapping[str, Any]) -> tuple[set[str], set[str], set[str], set[str]]:
    topics: set[str] = set()
    fragments: set[str] = set()
    results: set[str] = set()
    relations: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in ("topic_id", "topic_ids", "processed_topic_ids"):
                topics.update(str(item) for item in _as_list(value.get(key)) if str(item))
            for key in ("fragment_id", "fragment_ids", "processed_fragment_ids"):
                fragments.update(str(item) for item in _as_list(value.get(key)) if str(item))
            for key in ("result_id", "result_ids", "batch_result_id", "batch_result_ids", "processed_result_ids"):
                results.update(str(item) for item in _as_list(value.get(key)) if str(item))
            for key in ("relation_id", "relation_ids", "processed_relation_ids"):
                relations.update(str(item) for item in _as_list(value.get(key)) if str(item))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for key in ("topic_synthesis", "cross_group_comparison", "relation_candidates"):
        collect(request.get(key))
    return topics, fragments, results, relations


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


def _test_config(tmp_path: Path, *, min_effective_sections: int | None = None) -> Path:
    source = Path(__file__).resolve().parents[1] / "config.ini.example"
    target = tmp_path / "config.ini"
    parser = configparser.ConfigParser()
    parser.read(source, encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    parser["Preprocess"]["enabled"] = "true"
    parser["Preprocess"]["cache_dir"] = str(tmp_path / "preprocess-cache")
    parser["Preprocess"]["parser_mode"] = "local"
    parser["Preprocess"]["primary_parser"] = "local"
    parser["Preprocess"]["fallback_parser"] = "local"
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
    parser["Free_Mode_API"]["api_key"] = "outline-test"
    parser["Free_Mode_API"]["model"] = "outline-test"
    parser["Writer_API"]["api_key"] = "writer-test"
    parser["Writer_API"]["model"] = "writer-test"
    parser["Validator_API"]["api_key"] = "validator-test"
    parser["Validator_API"]["model"] = "validator-test"
    # These routes are intercepted at the fixture edge. Bind their declared
    # hosts to loopback as well, so CLI resume never requests external trust
    # for the example configuration's unrelated remote gateways.
    for section in (
        "Primary_Reader_API", "Backup_Reader_API", "Outline_API",
        "Free_Mode_API", "Writer_API", "Validator_API",
    ):
        parser[section]["api_base"] = "http://127.0.0.1:1/v1"
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["require_explicit_adoption"] = "true"
    if min_effective_sections is not None:
        parser["OutlineQualityGate"]["min_effective_sections"] = str(min_effective_sections)
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
        ("paper-a", "paper-a", "paper-a reports that the treatment improved the outcome."),
        ("paper-b", "paper-b", "paper-b reports that the treatment improved the outcome in a second context."),
        ("paper-c", "paper-c", "paper-c reports that the treatment improved the outcome under a third condition."),
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
    config_path = _test_config(tmp_path, min_effective_sections=2)
    _configure_writer_loopback(config_path, writer_api_base)

    spec = RuntimeJobSpec(
        project_name="current-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="current-e2e-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        # Finite fixture allowance for the complete Reader/Outline/Writer/
        # Validator chain; preserve its source and comparison coverage.
        metadata={
            "provider_budget": {
                "schema_version": "provider-aggregate-budget-contract-v2",
                "max_provider_calls_total": 64,
                "max_output_tokens_total": 2_000_000,
                "max_retry_attempts_total": 0,
                "max_wall_seconds": 1_800,
            },
        },
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
    (tmp_path / "source_bound_fixture_trace.json").write_text(
        json.dumps(fixture_trace, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    _assert_source_bound_fixture_trace(fixture_trace, require_candidate_source_bound=False)
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
        ("optional-a", "optional-a", "optional-a reports that the treatment improved the outcome."),
        ("optional-b", "optional-b", "optional-b reports that the treatment improved the outcome in a second context."),
        ("optional-c", "optional-c", "optional-c reports that the treatment improved the outcome under a third condition."),
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

    config_path = _test_config(tmp_path, min_effective_sections=2)
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
    _assert_source_bound_fixture_trace(fixture_trace, require_candidate_source_bound=False)
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

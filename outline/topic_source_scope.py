"""Pure selected-source identities for explicit successor topic requests.

These projections confer no Registry, PDF, provider or reuse authority. The
caller retains the original/current global publication bindings and verifies
the selected physical source pins separately before any reuse decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from outline.v3_models import (
    OutlineEvidenceView,
    OutlineEvidenceViews,
    PaperContentLayers,
    PaperEvidenceDossier,
    PaperIndexCard,
    TopicSynthesis,
    compute_v3_hash,
)
from runtime.provider_runtime import hash_json
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2


TOPIC_SOURCE_SELECTION_VERSION = "outline-topic-source-selection/v2"
TOPIC_BATCH_IDENTITY_VERSION = "outline-topic-batch-identity/v2"
TOPIC_REPLAY_SCOPE_VERSION = "source-revision-replay-scope/v1"


class TopicSourceScopeError(ValueError):
    """A topic request cannot establish one complete, unambiguous source scope."""


def _ids(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TopicSourceScopeError(f"{label} must be an ID array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TopicSourceScopeError(f"{label} contains an empty or non-string ID")
    if len(set(value)) != len(value) or (required and not value):
        raise TopicSourceScopeError(f"{label} contains duplicate IDs or lacks required IDs")
    return tuple(sorted(value))


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TopicSourceScopeError(f"{label} must be a nonempty string")
    return value


def _topic_identities(topics_or_request: Any) -> list[dict[str, Any]]:
    raw_topics = topics_or_request.get("topics") if isinstance(topics_or_request, Mapping) else topics_or_request
    if not isinstance(raw_topics, Sequence) or isinstance(raw_topics, (str, bytes)) or not raw_topics:
        raise TopicSourceScopeError("topics must be a nonempty array")
    identities: list[dict[str, Any]] = []
    seen_fragments: set[str] = set()
    for topic in raw_topics:
        if isinstance(topic, TopicSynthesis):
            # Read raw identity arrays before to_dict could deduplicate them.
            topic_id, fragment_id = topic.topic_id, topic.fragment_id
            papers, bridges = topic.paper_ids, topic.bridge_paper_ids
        elif isinstance(topic, Mapping):
            topic_id, fragment_id = topic.get("topic_id"), topic.get("fragment_id")
            papers, bridges = topic.get("paper_ids"), topic.get("bridge_paper_ids", [])
        else:
            raise TopicSourceScopeError("topic identity must be a mapping or TopicSynthesis")
        topic_id = _text(topic_id, "topic_id")
        fragment_id = _text(topic_id if fragment_id is None or fragment_id == "" else fragment_id, "fragment_id")
        if fragment_id in seen_fragments:
            raise TopicSourceScopeError("topics repeat a fragment identity")
        seen_fragments.add(fragment_id)
        identities.append({
            "topic_id": topic_id, "fragment_id": fragment_id,
            "paper_ids": list(_ids(papers, "paper_ids", required=True)),
            "bridge_paper_ids": list(_ids(bridges, "bridge_paper_ids")),
        })
    return sorted(identities, key=lambda row: (row["topic_id"], row["fragment_id"]))


def ownership_scope_v2(topics_or_request: Any) -> tuple[str, ...]:
    """Return the exact union of primary and bridge paper ownership."""
    return tuple(sorted({
        paper for row in _topic_identities(topics_or_request)
        for paper in (*row["paper_ids"], *row["bridge_paper_ids"])
    }))


def ownership_compatible_v2(left: Any, right: Any) -> bool:
    """Permit co-batching only when every fragment has the same owner set."""
    scopes = {
        tuple(sorted(set(row["paper_ids"]) | set(row["bridge_paper_ids"])))
        for row in [*_topic_identities(left), *_topic_identities(right)]
    }
    return len(scopes) == 1


def topic_source_replay_scope_v1(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Describe a v2 topic request for a later, separately verified reuse proof.

    The producer already binds the selected inventory's complete content hash.
    This projection retains that canonical identity separately from source-free
    task instructions. It does not validate source freshness or grant reuse.
    Legacy and other request kinds receive no new metadata.
    """
    if (
        request.get("provider_semantic_contract_version") != PROVIDER_SEMANTIC_CONTRACT_V2
        or request.get("semantic_contract_version") != "semantic-evidence-graph-v2"
        or request.get("task") != "substantive_topic_synthesis"
        or request.get("node_id") != "topic_synthesis"
    ):
        return None
    projection = request.get("evidence_projection")
    if not isinstance(projection, Mapping):
        raise TopicSourceScopeError("topic replay scope lacks its selected-source projection")
    selected = projection.get("selected_source_projection")
    if not isinstance(selected, Mapping) or selected.get("schema_version") != TOPIC_SOURCE_SELECTION_VERSION:
        raise TopicSourceScopeError("topic replay scope lacks its selected-source projection")
    digest = selected.get("content_hash")
    if (
        not isinstance(digest, str) or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise TopicSourceScopeError("topic replay scope requires a canonical selected-source content hash")
    papers = _ids(selected.get("paper_keys"), "selected-source paper_keys", required=True)
    if papers != ownership_scope_v2(request):
        raise TopicSourceScopeError("topic replay scope differs from canonical topic ownership")
    source_topic_fields = {
        "paper_ids", "bridge_paper_ids", "planned_evidence_unit_ids",
        "planned_evidence_ids", "required_evidence_count",
    }
    # Do not copy or hash every dossier again merely to describe instructions.
    # Unknown non-source fields stay included, so new instructions fail closed.
    non_source = {
        key: value for key, value in request.items()
        if key not in {"evidence_units", "planned_evidence_unit_ids", "topics", "evidence_projection"}
    }
    non_source["topics"] = [
        {key: value for key, value in topic.items() if key not in source_topic_fields}
        for topic in request["topics"]
    ]
    non_source["evidence_projection"] = {
        key: value for key, value in projection.items() if key != "selected_source_projection"
    }
    return {
        "schema_version": TOPIC_REPLAY_SCOPE_VERSION,
        "canonical_paper_keys": list(papers),
        "selected_source_content_hash": digest,
        "non_source_scope_hash": hash_json({
            "schema_version": "outline-topic-non-source-scope/v1", "request": non_source,
        }),
    }


def stable_topic_batch_identity_v2(topics_or_request: Any) -> dict[str, Any]:
    """Return ordinal-independent IDs plus the full collision-check identity.

    The 48-bit positive suffix retains the existing ``batch:[digits]`` shape.
    Planners must reject a repeated suffix with a different full identity_hash;
    the truncated suffix alone is never source or reuse authority.
    """
    body = {
        "schema_version": TOPIC_BATCH_IDENTITY_VERSION,
        "topics": _topic_identities(topics_or_request),
    }
    digest = hash_json(body)
    number = int(digest[:12], 16) or 1
    return {
        **body, "identity_hash": digest, "batch_number": number,
        "batch_id": f"topic_batch_{number}",
        "provider_node_id": f"topic_synthesis_provider:batch:{number}",
    }


def _index(rows: Any, expected_type: type, identity_field: str, label: str) -> dict[str, Any]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TopicSourceScopeError(f"{label} must be an array")
    result = {}
    for row in rows:
        if not isinstance(row, expected_type):
            raise TopicSourceScopeError(f"{label} contains an unsupported model")
        identity = _text(getattr(row, identity_field), label)
        if identity in result:
            raise TopicSourceScopeError(f"{label} repeats paper {identity}")
        result[identity] = row
    return result


def build_topic_source_selection_v2(
    request: Mapping[str, Any],
    *,
    evidence_model: OutlineEvidenceViews,
    content_layers_model: PaperContentLayers,
) -> dict[str, Any]:
    """Hash complete selected views/cards/dossiers without a whole-corpus hash.

    The request itself, including scientific units, qualifiers, locators,
    questions, dimensions and output rules, is neither rewritten nor reduced.
    Its complete provider payload remains a separate semantic-descriptor input.
    """
    if (not isinstance(request, Mapping)
            or request.get("semantic_contract_version") != "semantic-evidence-graph-v2"
            or request.get("task") != "substantive_topic_synthesis"
            or request.get("node_id") != "topic_synthesis"):
        raise TopicSourceScopeError("unsupported topic request contract")
    if (not isinstance(evidence_model, OutlineEvidenceViews)
            or not isinstance(content_layers_model, PaperContentLayers)
            or evidence_model.artifact_type != OutlineEvidenceViews.artifact_type
            or content_layers_model.artifact_type != PaperContentLayers.artifact_type
            or evidence_model.artifact_version != "v3" or content_layers_model.artifact_version != "v3"):
        raise TopicSourceScopeError("unsupported selected-source model contract")
    papers = ownership_scope_v2(request)
    views = _index(evidence_model.views, OutlineEvidenceView, "paper_key", "evidence views")
    cards = _index(content_layers_model.index_cards, PaperIndexCard, "paper_id", "index cards")
    dossiers = _index(content_layers_model.dossiers, PaperEvidenceDossier, "paper_id", "dossiers")
    rows = []
    selected_summary_hashes: set[str] = set()
    for paper in papers:
        if paper not in views or paper not in cards or paper not in dossiers:
            raise TopicSourceScopeError(f"selected paper {paper} lacks its view, index card or dossier")
        view, card, dossier = views[paper], cards[paper], dossiers[paper]
        lineage = _ids(view.source_summary_hashes, f"{paper}.source_summary_hashes", required=True)
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in lineage):
            raise TopicSourceScopeError(f"selected paper {paper} has invalid source-summary hashes")
        source_hash = _text(view.source_summary_hash, f"{paper}.source_summary_hash")
        expected_hash = (lineage[0] if len(lineage) == 1 else compute_v3_hash({
            "source_summary_hashes": list(view.source_summary_hashes),
        }))
        if (view.canonical_paper_key not in ("", paper) or dossier.dossier_id != f"dossier:{paper}"
                or card.evidence_package_id != dossier.dossier_id or source_hash != expected_hash
                or card.source_summary_hash != source_hash or dossier.source_summary_hash != source_hash
                or not set(lineage).issubset(evidence_model.source_summary_hashes)
                or not set(lineage).issubset(content_layers_model.source_summary_hashes)):
            raise TopicSourceScopeError(f"selected paper {paper} has foreign or inconsistent source lineage")
        if dossier.status not in {"ready", "partial"}:
            raise TopicSourceScopeError(f"selected paper {paper} has unsupported dossier status")
        selected_summary_hashes.update(lineage)
        rows.append({
            "paper_key": paper, "source_summary_hash": source_hash,
            "source_summary_hashes": list(lineage),
            "evidence_view": deepcopy(view.to_dict()), "evidence_view_hash": view.view_hash,
            "index_card": deepcopy(card.to_dict()),
            "dossier": deepcopy(dossier.to_dict()), "dossier_content_hash": dossier.content_hash,
        })
    units = request.get("evidence_units")
    if not isinstance(units, list) or not units or any(not isinstance(unit, Mapping) for unit in units):
        raise TopicSourceScopeError("topic request lacks complete evidence units")
    unit_ids: set[str] = set()
    unit_papers: set[str] = set()
    owner_by_unit: dict[str, str] = {}
    for unit in units:
        paper = _text(unit.get("paper_key"), "evidence unit paper_key")
        unit_id = _text(unit.get("evidence_unit_id"), "evidence_unit_id")
        if paper not in papers or unit_id in unit_ids:
            raise TopicSourceScopeError("topic evidence units repeat an ID or name a foreign paper")
        if (unit.get("source_summary_hash") != dossiers[paper].source_summary_hash
                or unit.get("evidence_unit_hash") != dossiers[paper].content_hash):
            raise TopicSourceScopeError("topic evidence unit has foreign source identity")
        unit_ids.add(unit_id)
        unit_papers.add(paper)
        owner_by_unit[unit_id] = paper
    if unit_papers != set(papers) or set(_ids(request.get("planned_evidence_unit_ids"), "planned_evidence_unit_ids", required=True)) != unit_ids:
        raise TopicSourceScopeError("topic planned/materialized source ownership is incomplete")
    for topic in request["topics"]:
        topic_papers = set(topic["paper_ids"]) | set(topic.get("bridge_paper_ids", []))
        planned = _ids(topic.get("planned_evidence_unit_ids"), "topic.planned_evidence_unit_ids", required=True)
        if any(unit_id not in owner_by_unit or owner_by_unit[unit_id] not in topic_papers for unit_id in planned):
            raise TopicSourceScopeError("topic evidence-unit membership names a foreign source")
    body = {
        "schema_version": TOPIC_SOURCE_SELECTION_VERSION,
        "paper_keys": list(papers), "papers": rows,
        "source_summary_hashes": sorted(selected_summary_hashes),
    }
    return {**body, "content_hash": hash_json(body)}

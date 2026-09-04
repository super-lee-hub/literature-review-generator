"""Deterministic opaque-alias layer for Outline provider boundaries (v1).

Provider-facing structural IDs are mapped from long canonical identities
(DOI / Chinese title keys / relation hashes) to short opaque tokens:

    P001..P0NN   -> canonical paper keys
    R001..R0NN   -> canonical relation ids

Provider prompts only ever see P/R tokens in structural fields, so the model
cannot fabricate, truncate, or echo external DOIs.  The alias map is a
durable artifact (outline_evidence_alias_map/v1) and participates in request
hashing (any change to the map changes every provider request hash).
"""
from __future__ import annotations

import hashlib
import re
import json
from typing import Any, Mapping, Sequence

OUTLINE_ALIAS_MAP_ARTIFACT_TYPE = "outline_evidence_alias_map"
OUTLINE_ALIAS_MAP_VERSION = "v1"

_PAPER_ALIAS_RE = re.compile(r"^P\d{3,5}$")
_RELATION_ALIAS_RE = re.compile(r"^R\d{3,5}$")

_STRUCTURAL_KEYS = ("paper_keys", "relation_ids", "paper_key", "relation_id")


def _token(prefix: str, index: int) -> str:
    return f"{prefix}{index:03d}"


def build_alias_map(
    paper_keys: Sequence[str],
    relation_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Deterministic alias map (order-stable, duplicates dropped)."""
    seen_papers: set[str] = set()
    paper_alias: dict[str, str] = {}
    for key in paper_keys:
        key = str(key).strip()
        if not key or key in seen_papers:
            continue
        seen_papers.add(key)
        paper_alias[key] = _token("P", len(paper_alias) + 1)
    seen_relations: set[str] = set()
    relation_alias: dict[str, str] = {}
    for rid in relation_ids:
        rid = str(rid).strip()
        if not rid or rid in seen_relations:
            continue
        seen_relations.add(rid)
        relation_alias[rid] = _token("R", len(relation_alias) + 1)
    payload = {
        "artifact_type": OUTLINE_ALIAS_MAP_ARTIFACT_TYPE,
        "artifact_version": OUTLINE_ALIAS_MAP_VERSION,
        "papers": paper_alias,
        "relations": relation_alias,
        "papers_reverse": {v: k for k, v in paper_alias.items()},
        "relations_reverse": {v: k for k, v in relation_alias.items()},
        "payload_sha256": "",
    }
    digest = hashlib.sha256(
        json.dumps(
            {
                "papers": paper_alias,
                "relations": relation_alias,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    payload["payload_sha256"] = digest
    return payload


def alias_for_paper(alias_map: Mapping[str, Any], canonical_key: str) -> str:
    token = (alias_map.get("papers") or {}).get(canonical_key)
    if not token:
        raise KeyError(f"no paper alias for {canonical_key!r}")
    return token


def alias_for_relation(alias_map: Mapping[str, Any], canonical_relation_id: str) -> str:
    token = (alias_map.get("relations") or {}).get(canonical_relation_id)
    if not token:
        raise KeyError(f"no relation alias for {canonical_relation_id!r}")
    return token


def canonical_for_alias(
    alias_map: Mapping[str, Any], alias_token: str
) -> str:
    alias_token = str(alias_token).strip()
    reverse = alias_map.get("papers_reverse") or {}
    if alias_token in reverse:
        return reverse[alias_token]
    raise KeyError(f"unknown paper alias {alias_token!r}")


def canonical_relation_for_alias(alias_map: Mapping[str, Any], alias_token: str) -> str:
    alias_token = str(alias_token).strip()
    reverse = alias_map.get("relations_reverse") or {}
    if alias_token in reverse:
        return reverse[alias_token]
    raise KeyError(f"unknown relation alias {alias_token!r}")


def _rewrite_ids(value: Any, alias_map: Mapping[str, Any], *, to_alias: bool) -> Any:
    """Rewrite structural ID fields inside a request/payload structure."""
    if isinstance(value, Mapping):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if to_alias:
                if key == "paper_key":
                    try:
                        item = alias_for_paper(alias_map, str(item))
                    except KeyError:
                        pass
                elif key == "relation_id":
                    try:
                        item = alias_for_relation(alias_map, str(item))
                    except KeyError:
                        pass
                elif key == "paper_keys" and isinstance(item, list):
                    item = _alias_id_list(item, alias_map, kind="paper")
                elif key == "relation_ids" and isinstance(item, list):
                    item = _alias_id_list(item, alias_map, kind="relation")
            else:
                if key == "paper_key" and str(item).strip() and _PAPER_ALIAS_RE.match(str(item).strip()):
                    try:
                        item = canonical_for_alias(alias_map, str(item))
                    except KeyError:
                        pass
                elif key == "relation_id" and str(item).strip() and _RELATION_ALIAS_RE.match(str(item).strip()):
                    try:
                        item = canonical_relation_for_alias(alias_map, str(item))
                    except KeyError:
                        pass
                elif key == "paper_keys" and isinstance(item, list):
                    item = _canonical_id_list(item, alias_map, kind="paper")
                elif key == "relation_ids" and isinstance(item, list):
                    item = _canonical_id_list(item, alias_map, kind="relation")
            rewritten[key] = _rewrite_ids(item, alias_map, to_alias=to_alias)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_ids(item, alias_map, to_alias=to_alias) for item in value]
    return value


def _alias_id_list(items: Sequence[Any], alias_map: Mapping[str, Any], *, kind: str) -> list[str]:
    out: list[str] = []
    for item in items:
        token = str(item).strip()
        if not token:
            continue
        try:
            out.append(
                alias_for_paper(alias_map, token)
                if kind == "paper"
                else alias_for_relation(alias_map, token)
            )
        except KeyError:
            out.append(token)  # unknown ids stay verbatim; validator must catch them
    return out


def _canonical_id_list(items: Sequence[Any], alias_map: Mapping[str, Any], *, kind: str) -> list[str]:
    out: list[str] = []
    for item in items:
        token = str(item).strip()
        if not token:
            continue
        if kind == "paper" and _PAPER_ALIAS_RE.match(token):
            try:
                out.append(canonical_for_alias(alias_map, token))
                continue
            except KeyError:
                out.append(token)
                continue
        if kind == "relation" and _RELATION_ALIAS_RE.match(token):
            try:
                out.append(canonical_relation_for_alias(alias_map, token))
                continue
            except KeyError:
                out.append(token)
                continue
        out.append(token)
    return out


def alias_structural(payload: Mapping[str, Any], alias_map: Mapping[str, Any]) -> dict[str, Any]:
    """Map canonical structural IDs to opaque P/R tokens for a provider request."""
    return _rewrite_ids(dict(payload), alias_map, to_alias=True)


def canonicalize_structural(payload: Mapping[str, Any], alias_map: Mapping[str, Any]) -> dict[str, Any]:
    """Map opaque P/R tokens back to canonical structural IDs in provider output."""
    return _rewrite_ids(dict(payload), alias_map, to_alias=False)


def is_paper_alias(token: str) -> bool:
    return bool(_PAPER_ALIAS_RE.match(str(token).strip()))


def is_relation_alias(token: str) -> bool:
    return bool(_RELATION_ALIAS_RE.match(str(token).strip()))

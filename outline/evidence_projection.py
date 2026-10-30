"""Outline Evidence Projection (v1).

Deterministic projection from authoritative Stage1 summary entries to the
compact provider-facing evidence pack consumed by Outline v3.

Why: Stage1 summary entries may embed full preprocess/stage1_input/visual
metadata (multi-MB blobs per paper).  Outline only needs the semantic summary
fields plus provenance.  Projection strips unrelated blobs and attaches a
per-entry source hash so the pack stays small while authority remains
traceable to the original Stage1 artifact bytes.

The pack itself is a durable artifact (outline_evidence_pack/v1): deterministic
given the same input entries in the same order.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable, Mapping

OUTLINE_EVIDENCE_PACK_ARTIFACT_TYPE = "outline_evidence_pack"
OUTLINE_EVIDENCE_PACK_VERSION = "v1"

# Fields Outline actually consumes (see outline.v3_evidence._extract_view and
# executor summaries usage).  Everything else in the Stage1 entry is unrelated
# to the provider payload and is projected away.
_KEPT_FIELDS = ("status", "source_mode", "paper_info", "ai_summary")


def _content_bound_paper_info(paper: Mapping[str, Any], digest: str) -> dict[str, Any]:
    """Normalize known primary-PDF locators; preserve all bibliographic text."""
    token = f"source-pdf:{digest}"
    path_fields = {"source_pdf", "pdf_path", "source_paper_id"}
    aliases = {str(paper[key]) for key in path_fields if paper.get(key)}
    descriptor = paper.get("source_descriptor")
    if isinstance(descriptor, Mapping):
        aliases.update(str(descriptor[key]) for key in path_fields if descriptor.get(key))
    names = {value.replace("\\", "/").rsplit("/", 1)[-1] for value in aliases}

    def normalize(value: Any, *, in_attachments: bool = False) -> Any:
        if isinstance(value, Mapping):
            return {
                key: (token if key in path_fields and isinstance(item, str) and item else
                      normalize(item, in_attachments=in_attachments or key == "attachments"))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [normalize(item, in_attachments=in_attachments) for item in value]
        if in_attachments and isinstance(value, str) and (
            value in aliases or value.replace("\\", "/").rsplit("/", 1)[-1] in names
        ):
            return token
        return deepcopy(value)

    result = normalize(paper)
    result["source_pdf"] = token
    result["source_pdf_sha256"] = digest
    result["source_pdf_fingerprint"] = digest
    return result


def entry_source_hash(entry: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON bytes of the original entry."""
    return hashlib.sha256(
        json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _entry_text_length(entry: Mapping[str, Any]) -> int:
    return len(json.dumps(entry, ensure_ascii=False))


def project_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project one Stage1 summary entry to the compact provider-facing shape."""
    if not isinstance(entry, Mapping):
        raise TypeError("summary entry must be a mapping")
    kept: dict[str, Any] = {}
    for field in _KEPT_FIELDS:
        value = entry.get(field)
        if value is not None:
            kept[field] = value
    if not isinstance(kept.get("ai_summary"), Mapping):
        raise ValueError("projected entry has no ai_summary mapping")
    if not isinstance(kept.get("paper_info"), Mapping):
        raise ValueError("projected entry has no paper_info mapping")
    provenance = {
        "source_entry_hash": entry_source_hash(entry),
        "source_entry_bytes": _entry_text_length(entry),
    }
    return {**kept, "provenance": provenance}


def project_entries(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [project_entry(entry) for entry in entries]


def pack_bytes(pack: Mapping[str, Any]) -> int:
    """Compact-pack JSON payload size (the part that feeds provider requests)."""
    entries = pack.get("entries") or []
    return len(json.dumps({"entries": entries}, ensure_ascii=False))


def build_pack(
    entries: Iterable[Mapping[str, Any]],
    *,
    source_ref: str = "",
    source_ref_sha256: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """Deterministic outline_evidence_pack/v1 from authoritative entries.

    Args:
        entries: Stage1 summary entries (order matters; kept in order).
        source_ref: path/identifier of the authoritative source file.
        source_ref_sha256: sha256 of that source file (provenance anchor).
        job_id: producer job id (informational).
    """
    projected = project_entries(entries)
    payload = {
        "artifact_type": OUTLINE_EVIDENCE_PACK_ARTIFACT_TYPE,
        "artifact_version": OUTLINE_EVIDENCE_PACK_VERSION,
        "entry_count": len(projected),
        "source_ref": str(source_ref or ""),
        "source_ref_sha256": str(source_ref_sha256 or ""),
        "source_job_id": str(job_id or ""),
        "entries": projected,
    }
    payload["pack_payload_sha256"] = hashlib.sha256(
        json.dumps({"entries": projected}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def build_source_closed_pack_v2(
    entries: Iterable[Mapping[str, Any]],
    *,
    source_pins: Mapping[str, Mapping[str, str]],
    source_ref: str = "",
    source_ref_sha256: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """Separate exact ingestion provenance from complete provider content.

    The runtime must verify these PDF pins against its registered SourceBundle
    and current bytes. This projection grants no source or reuse authority.
    Stage1 entries remain untouched. Legacy build_pack retains its v1 bytes.
    """
    projected = []
    audit = []
    for entry in entries:
        kept = deepcopy({key: entry[key] for key in _KEPT_FIELDS if key in entry})
        paper = kept.get("paper_info")
        if not isinstance(paper, dict) or not isinstance(kept.get("ai_summary"), Mapping):
            raise ValueError("source-closed entry requires paper identity and complete summary")
        key = str(paper.get("canonical_paper_key") or "")
        pin = source_pins.get(key)
        if not isinstance(pin, Mapping):
            raise ValueError("source-closed entry has no runtime PDF pin")
        digest = str(pin.get("source_pdf_sha256") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("source-closed entry PDF pin is invalid")
        for field in ("source_pdf_sha256", "source_pdf_fingerprint"):
            if paper.get(field) and paper[field] != digest:
                raise ValueError("source-closed entry differs from its runtime PDF pin")
        audit.append({
            "canonical_paper_key": key,
            "source_entry_hash": entry_source_hash(entry),
            "source_entry_bytes": len(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "original_provenance": deepcopy(entry.get("provenance")),
            "original_paper_info": deepcopy(paper),
            "original_source_pdf": paper.get("source_pdf"),
            "runtime_source_pdf": pin.get("source_pdf"),
            "source_pdf_sha256": digest,
        })
        kept["paper_info"] = _content_bound_paper_info(paper, digest)
        semantic_bytes = json.dumps(kept, ensure_ascii=False, sort_keys=True).encode("utf-8")
        projected.append({**kept, "provenance": {
            "schema_version": "source-closed-entry-content/v2",
            "semantic_entry_sha256": hashlib.sha256(semantic_bytes).hexdigest(),
            "semantic_entry_bytes": len(semantic_bytes),
        }})
    payload = {
        "artifact_type": OUTLINE_EVIDENCE_PACK_ARTIFACT_TYPE, "artifact_version": "v2",
        "entry_count": len(projected), "source_ref": source_ref,
        "source_ref_sha256": source_ref_sha256, "source_job_id": job_id,
        "entries": projected, "source_entry_provenance": audit,
    }
    payload["pack_payload_sha256"] = hashlib.sha256(
        json.dumps({"entries": projected}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    validate_pack(payload)
    return payload


def validate_pack(pack: Mapping[str, Any]) -> None:
    """Validate a built pack (used by consumers and tests)."""
    if pack.get("artifact_type") != OUTLINE_EVIDENCE_PACK_ARTIFACT_TYPE:
        raise ValueError("not an outline_evidence_pack")
    if pack.get("artifact_version") not in (OUTLINE_EVIDENCE_PACK_VERSION, "v2"):
        raise ValueError("unsupported outline_evidence_pack version")
    entries = pack.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("outline_evidence_pack has no entries")
    if pack.get("entry_count") != len(entries):
        raise ValueError("outline_evidence_pack entry_count mismatch")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("outline_evidence_pack entry is not an object")
        provenance = entry.get("provenance")
        provenance_key = "semantic_entry_sha256" if pack.get("artifact_version") == "v2" else "source_entry_hash"
        if not isinstance(provenance, Mapping) or not provenance.get(provenance_key):
            raise ValueError("outline_evidence_pack entry has no source provenance")
        paper_info = entry.get("paper_info")
        if not isinstance(paper_info, Mapping):
            raise ValueError("outline_evidence_pack entry has no paper_info")
        key = str(paper_info.get("canonical_paper_key") or "")
        if not key or key in seen:
            raise ValueError("outline_evidence_pack entry key missing or duplicated")
        seen.add(key)
    if pack.get("artifact_version") == "v2":
        audit = pack.get("source_entry_provenance")
        if not isinstance(audit, list) or any(not isinstance(row, Mapping) for row in audit) or [row.get("canonical_paper_key") for row in audit] != [
            entry["paper_info"]["canonical_paper_key"] for entry in entries
        ]:
            raise ValueError("source-closed evidence pack ingestion provenance is incomplete")
        for entry in entries:
            semantic = {key: value for key, value in entry.items() if key != "provenance"}
            raw = json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if entry["provenance"] != {
                "schema_version": "source-closed-entry-content/v2",
                "semantic_entry_sha256": hashlib.sha256(raw).hexdigest(),
                "semantic_entry_bytes": len(raw),
            }:
                raise ValueError("source-closed evidence pack semantic identity differs")
        expected = hashlib.sha256(
            json.dumps({"entries": entries}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if pack.get("pack_payload_sha256") != expected:
            raise ValueError("source-closed evidence pack payload hash differs")


def load_pack_from_file(path: str) -> dict[str, Any]:
    """Load and validate a persisted pack file."""
    from pathlib import Path  # local import keeps module lightweight

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_pack(payload)
    return payload

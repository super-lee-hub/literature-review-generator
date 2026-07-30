from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping, Sequence
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime.reconcile import (  # noqa: E402
    ReconcileValidationError,
    validate_canonical_ai_summary,
)
from services.citation_ref_catalog import (  # noqa: E402
    build_document_ref_catalog,
    validate_document_ref_catalog,
)
from services.job_workspace import atomic_write_json  # noqa: E402
from services.paper_identity import normalize_doi  # noqa: E402


PLAN_SCHEMA = "pph_kalyanaram_supplemental_refresh_plan_v1"
TARGET_DOI = "10.1287/mksc.14.3.g161"

PROJECTS: dict[str, tuple[str, str]] = {
    "S02": ("pph_s02_prior_concession", "20260728_063103_df0fe480"),
    "S03": ("pph_s03_concession_to_unfairness", "20260728_063453_5344a69b"),
    "S05": ("pph_s05_subjective_knowledge", "20260728_063507_e48eec64"),
}

# These are the frozen pre-refresh cardinalities observed in the accepted PPH
# corpus. A default CLI run refuses to plan against a different corpus head.
DEFAULT_EXPECTED_PRE_COUNTS: dict[str, int] = {
    "master_summaries": 141,
    "subset_02": 1,
    "subset_90": 2,
    "subset_91": 6,
    "subset_03": 3,
    "subset_05": 7,
    "s02_summary": 9,
    "s02_catalog": 9,
    "s03_summary": 13,
    "s03_catalog": 13,
    "s05_summary": 10,
    "s05_catalog": 10,
}


class RefreshError(RuntimeError):
    pass


@dataclass(frozen=True)
class RefreshPaths:
    repo_root: Path
    master_summaries: Path
    subset_02: Path
    subset_90: Path
    subset_91: Path
    subset_03: Path
    subset_05: Path
    s02_summary: Path
    s02_catalog: Path
    s03_summary: Path
    s03_catalog: Path
    s05_summary: Path
    s05_catalog: Path

    @classmethod
    def from_repo_root(cls, repo_root: str | Path) -> "RefreshPaths":
        root = Path(repo_root).expanduser().resolve()
        work = root / "output" / "pph_review_work"

        def workspace(project_id: str) -> Path:
            project_name, job_id = PROJECTS[project_id]
            return root / "output" / f"{project_name}__{job_id}"

        def summary(project_id: str) -> Path:
            project_name, _ = PROJECTS[project_id]
            return (
                workspace(project_id) / "artifacts" / f"{project_name}_summaries.json"
            )

        def catalog(project_id: str) -> Path:
            project_name, _ = PROJECTS[project_id]
            return (
                workspace(project_id)
                / "artifacts"
                / "citation_catalogs"
                / f"{project_name}_citation_ref_catalog.json"
            )

        return cls(
            repo_root=root,
            master_summaries=work / "master_summaries.json",
            subset_02=work / "subset_summaries" / "02_summaries.json",
            subset_90=work / "subset_summaries" / "90_summaries.json",
            subset_91=work / "subset_summaries" / "91_summaries.json",
            subset_03=work / "subset_summaries" / "03_summaries.json",
            subset_05=work / "subset_summaries" / "05_summaries.json",
            s02_summary=summary("S02"),
            s02_catalog=catalog("S02"),
            s03_summary=summary("S03"),
            s03_catalog=catalog("S03"),
            s05_summary=summary("S05"),
            s05_catalog=catalog("S05"),
        )

    def summary_inputs(self) -> dict[str, Path]:
        return {
            "master_summaries": self.master_summaries,
            "subset_02": self.subset_02,
            "subset_90": self.subset_90,
            "subset_91": self.subset_91,
            "subset_03": self.subset_03,
            "subset_05": self.subset_05,
            "s02_summary": self.s02_summary,
            "s03_summary": self.s03_summary,
            "s05_summary": self.s05_summary,
        }

    def catalog_inputs(self) -> dict[str, Path]:
        return {
            "s02_catalog": self.s02_catalog,
            "s03_catalog": self.s03_catalog,
            "s05_catalog": self.s05_catalog,
        }


@dataclass(frozen=True)
class PreparedRefresh:
    plan: dict[str, Any]
    payloads: dict[str, Any]
    paths: dict[str, Path]


def _json_file_bytes(payload: Any) -> bytes:
    """Predict bytes emitted by job_workspace.atomic_write_json text mode."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if os.linesep != "\n":
        text = text.replace("\n", os.linesep)
    return text.encode("utf-8")


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_sha256(payload: Any) -> str:
    return hashlib.sha256(_json_file_bytes(payload)).hexdigest()


def _load_json(path: Path, *, label: str) -> Any:
    if not path.is_file():
        raise RefreshError(f"{label} does not exist or is not a file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RefreshError(f"{label} is not valid UTF-8 JSON: {path}: {exc}") from exc


def _require_under_root(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.expanduser().resolve())
    except ValueError as exc:
        raise RefreshError(f"{label} escapes the repository root: {resolved}") from exc
    return resolved


def _paper_info(record: Mapping[str, Any], *, label: str) -> Mapping[str, Any]:
    paper_info = record.get("paper_info")
    if not isinstance(paper_info, Mapping):
        raise RefreshError(f"{label} has no paper_info object")
    return paper_info


def _record_doi(record: Mapping[str, Any]) -> str:
    paper_info = record.get("paper_info")
    if not isinstance(paper_info, Mapping):
        return ""
    return normalize_doi(paper_info.get("doi"))


def _record_key(record: Mapping[str, Any], *, label: str) -> str:
    paper_info = _paper_info(record, label=label)
    doi = normalize_doi(paper_info.get("doi"))
    if doi:
        return f"doi:{doi}"
    canonical_key = str(paper_info.get("canonical_paper_key") or "").strip()
    if not canonical_key:
        raise RefreshError(f"{label} has neither a valid DOI nor canonical_paper_key")
    return f"key:{canonical_key.casefold()}"


def _validate_summary_record(record: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise RefreshError(f"{label} must be a JSON object")
    materialized = dict(record)
    if str(materialized.get("status") or "").strip().casefold() != "success":
        raise RefreshError(f"{label} is not a successful Stage 1 summary")
    paper_info = _paper_info(materialized, label=label)
    canonical_key = str(paper_info.get("canonical_paper_key") or "").strip()
    if not canonical_key:
        raise RefreshError(f"{label} paper_info.canonical_paper_key is required")
    try:
        validate_canonical_ai_summary(
            materialized.get("ai_summary"),
            label=f"{label} ai_summary",
        )
    except ReconcileValidationError as exc:
        raise RefreshError(str(exc)) from exc
    return materialized


def _validate_summary_list(payload: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise RefreshError(f"{label} must contain a non-empty JSON array")
    records = [
        _validate_summary_record(record, label=f"{label} entry {index}")
        for index, record in enumerate(payload)
    ]
    _assert_unique_records(records, label=label)
    return records


def _assert_unique_records(records: Sequence[Mapping[str, Any]], *, label: str) -> None:
    identity_indexes: dict[str, int] = {}
    canonical_indexes: dict[str, int] = {}
    for index, record in enumerate(records):
        identity = _record_key(record, label=f"{label} entry {index}")
        if identity in identity_indexes:
            raise RefreshError(
                f"{label} has duplicate paper identity at entries "
                f"{identity_indexes[identity]} and {index}: {identity}"
            )
        identity_indexes[identity] = index
        paper_info = _paper_info(record, label=f"{label} entry {index}")
        canonical_key = (
            str(paper_info.get("canonical_paper_key") or "").strip().casefold()
        )
        if canonical_key in canonical_indexes:
            raise RefreshError(
                f"{label} has duplicate canonical_paper_key at entries "
                f"{canonical_indexes[canonical_key]} and {index}: {canonical_key}"
            )
        canonical_indexes[canonical_key] = index


def _load_source_record(path: Path) -> dict[str, Any]:
    payload = _load_json(path, label="Stage 1 source summary")
    if not isinstance(payload, list) or len(payload) != 1:
        raise RefreshError(
            "Stage 1 source summary must be a canonical JSON array containing exactly one record"
        )
    record = _validate_summary_record(
        payload[0], label="Stage 1 source summary entry 0"
    )
    paper_info = _paper_info(record, label="Stage 1 source summary entry 0")
    doi = normalize_doi(paper_info.get("doi"))
    if doi != TARGET_DOI:
        raise RefreshError(
            f"Stage 1 source DOI must be {TARGET_DOI}, got {doi or '<invalid-or-missing>'}"
        )
    ai_summary = record.get("ai_summary")
    metadata = (
        ai_summary.get("paper_metadata") if isinstance(ai_summary, Mapping) else None
    )
    metadata_doi = (
        normalize_doi(metadata.get("doi")) if isinstance(metadata, Mapping) else ""
    )
    if metadata_doi and metadata_doi != TARGET_DOI:
        raise RefreshError(
            "Stage 1 source paper_info DOI conflicts with ai_summary.paper_metadata DOI"
        )
    return record


def _merge_new_record(
    records: Sequence[Mapping[str, Any]],
    incoming: Mapping[str, Any],
    *,
    label: str,
) -> list[dict[str, Any]]:
    incoming_doi = _record_doi(incoming)
    matches = [
        index
        for index, record in enumerate(records)
        if _record_doi(record) == incoming_doi
    ]
    if matches:
        raise RefreshError(
            f"{label} already contains DOI {incoming_doi}; refusing a non-fresh supplemental merge"
        )
    merged = [dict(record) for record in records]
    merged.append(dict(incoming))
    _assert_unique_records(merged, label=label)
    return merged


def _dedupe_exact(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    *,
    label: str,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_identity: dict[str, dict[str, Any]] = {}
    for group in groups:
        for record in group:
            item = dict(record)
            identity = _record_key(item, label=label)
            existing = by_identity.get(identity)
            if existing is None:
                by_identity[identity] = item
                merged.append(item)
                continue
            if _canonical_hash(existing) != _canonical_hash(item):
                raise RefreshError(
                    f"{label} contains conflicting records for paper identity {identity}"
                )
    _assert_unique_records(merged, label=label)
    return merged


def _assert_same_records(
    actual: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> None:
    if _canonical_hash(list(actual)) != _canonical_hash(list(expected)):
        raise RefreshError(f"{label} is not the expected dependency materialization")


def _assert_records_in_master(
    master: Sequence[Mapping[str, Any]],
    groups: Sequence[Sequence[Mapping[str, Any]]],
) -> None:
    master_by_identity = {
        _record_key(record, label="master_summaries"): dict(record) for record in master
    }
    for group in groups:
        for record in group:
            identity = _record_key(record, label="subset summary")
            master_record = master_by_identity.get(identity)
            if master_record is None:
                raise RefreshError(
                    f"master_summaries is missing subset identity {identity}"
                )
            if _canonical_hash(master_record) != _canonical_hash(dict(record)):
                raise RefreshError(
                    f"master_summaries conflicts with subset identity {identity}"
                )


def _load_catalog(
    path: Path,
    *,
    label: str,
    project_name: str,
    job_id: str,
) -> dict[str, Any]:
    payload = _load_json(path, label=label)
    if not isinstance(payload, Mapping):
        raise RefreshError(f"{label} must be a JSON object")
    try:
        catalog = validate_document_ref_catalog(payload)
    except ValueError as exc:
        raise RefreshError(f"{label} is invalid: {exc}") from exc
    if catalog.get("created_from_job_id") != job_id:
        raise RefreshError(f"{label} created_from_job_id does not match {job_id}")
    expected_catalog_id = f"citation_ref_catalog:{project_name}"
    if catalog.get("catalog_id") != expected_catalog_id:
        raise RefreshError(f"{label} catalog_id does not match {expected_catalog_id}")
    return catalog


def _active_catalog_entries(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(entry)
        for entry in catalog.get("entries", [])
        if isinstance(entry, Mapping) and entry.get("status") == "active"
    ]


def _catalog_identity(entry: Mapping[str, Any]) -> str:
    doi = normalize_doi(entry.get("doi"))
    if doi:
        return f"doi:{doi}"
    canonical_key = str(entry.get("canonical_paper_key") or "").strip()
    if not canonical_key:
        raise RefreshError("citation catalog entry has no stable identity")
    return f"key:{canonical_key.casefold()}"


def _max_ref_number(catalog: Mapping[str, Any]) -> int:
    numbers = []
    for entry in catalog.get("entries", []):
        if not isinstance(entry, Mapping):
            continue
        match = re.fullmatch(r"R(\d{3,})", str(entry.get("ref_id") or ""))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0)


def _build_catalog(
    summaries: Sequence[Mapping[str, Any]],
    existing: Mapping[str, Any],
    *,
    project_id: str,
) -> tuple[dict[str, Any], str]:
    project_name, job_id = PROJECTS[project_id]
    old_entries = _active_catalog_entries(existing)
    old_by_identity = {_catalog_identity(entry): entry for entry in old_entries}
    if f"doi:{TARGET_DOI}" in old_by_identity:
        raise RefreshError(f"{project_id} catalog already contains DOI {TARGET_DOI}")

    catalog = build_document_ref_catalog(
        summaries,
        project_name=project_name,
        job_id=job_id,
        existing_catalog=existing,
    )
    try:
        validate_document_ref_catalog(catalog)
    except ValueError as exc:
        raise RefreshError(f"generated {project_id} catalog is invalid: {exc}") from exc

    new_entries = _active_catalog_entries(catalog)
    new_by_identity = {_catalog_identity(entry): entry for entry in new_entries}
    for identity, old_entry in old_by_identity.items():
        new_entry = new_by_identity.get(identity)
        if new_entry is None:
            raise RefreshError(f"generated {project_id} catalog dropped {identity}")
        if new_entry.get("ref_id") != old_entry.get("ref_id"):
            raise RefreshError(
                f"generated {project_id} catalog changed ref_id for {identity}"
            )

    source_entry = new_by_identity.get(f"doi:{TARGET_DOI}")
    expected_ref_id = f"R{_max_ref_number(existing) + 1:03d}"
    if source_entry is None or source_entry.get("ref_id") != expected_ref_id:
        raise RefreshError(
            f"generated {project_id} catalog did not append {TARGET_DOI} as {expected_ref_id}"
        )
    if len(new_entries) != len(old_entries) + 1:
        raise RefreshError(
            f"generated {project_id} catalog active count did not increase by one"
        )
    if len(new_entries) != len(summaries):
        raise RefreshError(
            f"generated {project_id} catalog does not cover every summary"
        )
    return catalog, expected_ref_id


def _summary_state(path: Path, payload: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "path": str(path),
        "kind": "summary_file",
        "count": len(payload),
        "sha256": _file_sha256(path),
    }


def _catalog_state(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path),
        "kind": "citation_ref_catalog",
        "count": len(payload.get("entries", [])),
        "active_count": len(_active_catalog_entries(payload)),
        "sha256": _file_sha256(path),
        "catalog_hash": str(payload.get("catalog_hash") or ""),
    }


def _after_state(path: Path, payload: Any, *, kind: str) -> dict[str, Any]:
    state: dict[str, Any] = {
        "path": str(path),
        "kind": kind,
        "sha256": _payload_sha256(payload),
    }
    if kind == "summary_file":
        state["count"] = len(payload)
    else:
        state["count"] = len(payload.get("entries", []))
        state["active_count"] = len(_active_catalog_entries(payload))
        state["catalog_hash"] = str(payload.get("catalog_hash") or "")
    return state


def _validate_expected_count(
    role: str,
    state: Mapping[str, Any],
    expected_counts: Mapping[str, int] | None,
) -> None:
    if expected_counts is None:
        return
    if role not in expected_counts:
        raise RefreshError(f"missing expected pre-refresh count for {role}")
    field = "active_count" if state.get("kind") == "citation_ref_catalog" else "count"
    actual = int(state[field])
    expected = int(expected_counts[role])
    if actual != expected:
        raise RefreshError(
            f"{role} expected pre-refresh count {expected}, got {actual}"
        )


def _plan_hash(plan_without_hash: Mapping[str, Any]) -> str:
    return _canonical_hash(plan_without_hash)


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    if payload.get("schema_version") != PLAN_SCHEMA:
        raise RefreshError("refresh plan schema_version is invalid")
    declared_hash = str(payload.pop("plan_hash", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
        raise RefreshError("refresh plan has no valid plan_hash")
    expected_hash = _plan_hash(payload)
    if declared_hash != expected_hash:
        raise RefreshError("refresh plan_hash does not match its payload")
    payload["plan_hash"] = declared_hash
    return payload


def prepare_refresh(
    paths: RefreshPaths,
    source_summary: str | Path,
    *,
    expected_pre_counts: Mapping[str, int] | None = None,
) -> PreparedRefresh:
    root = paths.repo_root.expanduser().resolve()
    path_map = {**paths.summary_inputs(), **paths.catalog_inputs()}
    resolved_paths = {
        role: _require_under_root(path, root, label=role)
        for role, path in path_map.items()
    }
    if len(set(resolved_paths.values())) != len(resolved_paths):
        raise RefreshError("refresh path map contains duplicate canonical targets")

    source_path = Path(source_summary).expanduser().resolve()
    source_record = _load_source_record(source_path)

    summaries: dict[str, list[dict[str, Any]]] = {}
    summary_states: dict[str, dict[str, Any]] = {}
    for role in paths.summary_inputs():
        path = resolved_paths[role]
        payload = _validate_summary_list(_load_json(path, label=role), label=role)
        state = _summary_state(path, payload)
        _validate_expected_count(role, state, expected_pre_counts)
        if any(_record_doi(record) == TARGET_DOI for record in payload):
            raise RefreshError(f"{role} already contains DOI {TARGET_DOI}")
        summaries[role] = payload
        summary_states[role] = state

    catalogs: dict[str, dict[str, Any]] = {}
    catalog_states: dict[str, dict[str, Any]] = {}
    for project_id in ("S02", "S03", "S05"):
        role = f"{project_id.casefold()}_catalog"
        project_name, job_id = PROJECTS[project_id]
        path = resolved_paths[role]
        payload = _load_catalog(
            path,
            label=role,
            project_name=project_name,
            job_id=job_id,
        )
        state = _catalog_state(path, payload)
        _validate_expected_count(role, state, expected_pre_counts)
        catalogs[role] = payload
        catalog_states[role] = state

    _assert_records_in_master(
        summaries["master_summaries"],
        [
            summaries["subset_02"],
            summaries["subset_90"],
            summaries["subset_91"],
            summaries["subset_03"],
            summaries["subset_05"],
        ],
    )
    expected_s02_before = _dedupe_exact(
        [summaries["subset_02"], summaries["subset_90"], summaries["subset_91"]],
        label="S02 pre-refresh dependency materialization",
    )
    _assert_same_records(
        summaries["s02_summary"],
        expected_s02_before,
        label="s02_summary",
    )
    expected_s05_before = _dedupe_exact(
        [summaries["subset_05"], summaries["subset_02"], summaries["subset_03"]],
        label="S05 pre-refresh dependency materialization",
    )
    _assert_same_records(
        summaries["s05_summary"],
        expected_s05_before,
        label="s05_summary",
    )

    master_after = _merge_new_record(
        summaries["master_summaries"], source_record, label="master_summaries"
    )
    subset_02_after = _merge_new_record(
        summaries["subset_02"], source_record, label="subset_02"
    )
    s02_after = _dedupe_exact(
        [subset_02_after, summaries["subset_90"], summaries["subset_91"]],
        label="S02 post-refresh dependency materialization",
    )
    s03_after = _merge_new_record(
        summaries["s03_summary"], source_record, label="s03_summary"
    )
    s05_after = _dedupe_exact(
        [summaries["subset_05"], subset_02_after, summaries["subset_03"]],
        label="S05 post-refresh dependency materialization",
    )

    if len(s02_after) != len(summaries["s02_summary"]) + 1:
        raise RefreshError("S02 post-refresh count did not increase by exactly one")
    if len(s05_after) != len(summaries["s05_summary"]) + 1:
        raise RefreshError("S05 post-refresh count did not increase by exactly one")

    s02_catalog_after, s02_ref = _build_catalog(
        s02_after, catalogs["s02_catalog"], project_id="S02"
    )
    s03_catalog_after, s03_ref = _build_catalog(
        s03_after, catalogs["s03_catalog"], project_id="S03"
    )
    s05_catalog_after, s05_ref = _build_catalog(
        s05_after, catalogs["s05_catalog"], project_id="S05"
    )

    payloads: dict[str, Any] = {
        "master_summaries": master_after,
        "subset_02": subset_02_after,
        "s02_summary": s02_after,
        "s02_catalog": s02_catalog_after,
        "s03_summary": s03_after,
        "s03_catalog": s03_catalog_after,
        "s05_summary": s05_after,
        "s05_catalog": s05_catalog_after,
    }
    write_paths = {role: resolved_paths[role] for role in payloads}

    read_only_roles = ("subset_90", "subset_91", "subset_03", "subset_05")
    inputs = [summary_states[role] | {"role": role} for role in read_only_roles]
    writes = []
    for role, payload in payloads.items():
        before = summary_states.get(role) or catalog_states.get(role)
        if before is None:
            raise AssertionError(f"missing pre-refresh state for {role}")
        writes.append(
            {
                "role": role,
                "path": str(write_paths[role]),
                "kind": before["kind"],
                "before": {
                    key: value for key, value in before.items() if key != "path"
                },
                "after": {
                    key: value
                    for key, value in _after_state(
                        write_paths[role], payload, kind=str(before["kind"])
                    ).items()
                    if key != "path"
                },
            }
        )

    source_paper_info = _paper_info(
        source_record, label="Stage 1 source summary entry 0"
    )
    plan_without_hash: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "purpose": "20260729 Kalyanaram supplemental Stage 1 refresh",
        "target_doi": TARGET_DOI,
        "repo_root": str(root),
        "source": {
            "path": str(source_path),
            "sha256": _file_sha256(source_path),
            "record_hash": _canonical_hash(source_record),
            "canonical_paper_key": str(
                source_paper_info.get("canonical_paper_key") or ""
            ),
        },
        "inputs": inputs,
        "writes": writes,
        "catalog_ref_ids": {
            "S02": s02_ref,
            "S03": s03_ref,
            "S05": s05_ref,
        },
        "requires_post_refresh_reconcile": ["S02", "S03", "S05"],
    }
    plan = dict(plan_without_hash)
    plan["plan_hash"] = _plan_hash(plan_without_hash)
    return PreparedRefresh(plan=plan, payloads=payloads, paths=write_paths)


def _expected_file_state(plan: Mapping[str, Any]) -> dict[Path, dict[str, Any]]:
    expected: dict[Path, dict[str, Any]] = {}
    for item in plan.get("inputs", []):
        if not isinstance(item, Mapping):
            raise RefreshError("refresh plan contains an invalid input state")
        expected[Path(str(item.get("path") or "")).resolve()] = dict(item)
    for item in plan.get("writes", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("before"), Mapping):
            raise RefreshError("refresh plan contains an invalid write state")
        state = dict(item["before"])
        state["path"] = str(item.get("path") or "")
        expected[Path(state["path"]).resolve()] = state
    return expected


def _assert_pre_state(plan: Mapping[str, Any]) -> None:
    source = plan.get("source")
    if not isinstance(source, Mapping):
        raise RefreshError("refresh plan source is invalid")
    source_path = Path(str(source.get("path") or "")).resolve()
    if _file_sha256(source_path) != source.get("sha256"):
        raise RefreshError("Stage 1 source hash drifted after planning")
    for path, state in _expected_file_state(plan).items():
        if not path.is_file():
            raise RefreshError(f"planned input disappeared: {path}")
        if _file_sha256(path) != state.get("sha256"):
            raise RefreshError(f"planned input hash drifted: {path}")


def _write_staged_payload(path: Path, payload: Any, expected_hash: str) -> Path:
    staged = path.with_name(f".{path.name}.kalyanaram-{uuid.uuid4().hex}.tmp")
    atomic_write_json(str(staged), payload)
    if _file_sha256(staged) != expected_hash:
        staged.unlink(missing_ok=True)
        raise RefreshError(f"staged payload hash mismatch for {path}")
    return staged


def _backup_targets(
    prepared: PreparedRefresh,
    *,
    backup_root: Path,
) -> tuple[Path, dict[str, Path]]:
    plan_hash = str(prepared.plan["plan_hash"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / f"{timestamp}_{plan_hash[:12]}"
    if backup_dir.exists():
        raise RefreshError(f"backup directory already exists: {backup_dir}")
    files_dir = backup_dir / "files"
    files_dir.mkdir(parents=True)
    backups: dict[str, Path] = {}
    rows = []
    for index, (role, path) in enumerate(prepared.paths.items()):
        backup = files_dir / f"{index:02d}_{role}{path.suffix}"
        shutil.copy2(path, backup)
        expected_hash = next(
            item["before"]["sha256"]
            for item in prepared.plan["writes"]
            if item["role"] == role
        )
        if _file_sha256(backup) != expected_hash:
            raise RefreshError(f"backup hash mismatch for {role}")
        backups[role] = backup
        rows.append(
            {
                "role": role,
                "original_path": str(path),
                "backup_path": str(backup),
                "sha256": expected_hash,
            }
        )
    atomic_write_json(
        str(backup_dir / "backup_manifest.json"),
        {
            "schema_version": "pph_kalyanaram_refresh_backup_v1",
            "status": "prepared",
            "plan": prepared.plan,
            "files": rows,
        },
    )
    return backup_dir, backups


def _restore_backups(
    prepared: PreparedRefresh,
    backups: Mapping[str, Path],
) -> None:
    errors: list[str] = []
    expected_states = {
        item["role"]: item["before"] for item in prepared.plan.get("writes", [])
    }
    for role, target in prepared.paths.items():
        backup = backups.get(role)
        if backup is None:
            errors.append(f"missing backup for {role}")
            continue
        restore_stage = target.with_name(
            f".{target.name}.restore-{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copy2(backup, restore_stage)
            os.replace(restore_stage, target)
            expected_hash = expected_states[role]["sha256"]
            if _file_sha256(target) != expected_hash:
                errors.append(f"restored hash mismatch for {role}")
        except Exception as exc:  # pragma: no cover - catastrophic filesystem path
            errors.append(f"{role}: {type(exc).__name__}: {exc}")
        finally:
            restore_stage.unlink(missing_ok=True)
    if errors:
        raise RefreshError("rollback failed: " + "; ".join(errors))


def _verify_post_state(prepared: PreparedRefresh) -> None:
    write_items = {item["role"]: item for item in prepared.plan["writes"]}
    for role, path in prepared.paths.items():
        expected = write_items[role]["after"]
        if _file_sha256(path) != expected["sha256"]:
            raise RefreshError(f"post-refresh hash mismatch for {role}")
        payload = _load_json(path, label=f"post-refresh {role}")
        if expected["kind"] == "summary_file":
            records = _validate_summary_list(payload, label=f"post-refresh {role}")
            if len(records) != expected["count"]:
                raise RefreshError(f"post-refresh count mismatch for {role}")
        else:
            if not isinstance(payload, Mapping):
                raise RefreshError(f"post-refresh {role} is not a catalog object")
            validate_document_ref_catalog(payload)
            if len(payload.get("entries", [])) != expected["count"]:
                raise RefreshError(f"post-refresh catalog count mismatch for {role}")
            if len(_active_catalog_entries(payload)) != expected["active_count"]:
                raise RefreshError(
                    f"post-refresh active catalog count mismatch for {role}"
                )
            if payload.get("catalog_hash") != expected["catalog_hash"]:
                raise RefreshError(f"post-refresh catalog_hash mismatch for {role}")


def apply_refresh(
    paths: RefreshPaths,
    source_summary: str | Path,
    expected_plan: Mapping[str, Any],
    *,
    expected_pre_counts: Mapping[str, int] | None = None,
    backup_root: str | Path | None = None,
) -> dict[str, Any]:
    accepted_plan = validate_plan(expected_plan)
    prepared = prepare_refresh(
        paths,
        source_summary,
        expected_pre_counts=expected_pre_counts,
    )
    if _canonical_hash(prepared.plan) != _canonical_hash(accepted_plan):
        raise RefreshError(
            "current refresh plan does not match the accepted dry-run plan"
        )
    _assert_pre_state(prepared.plan)

    root = paths.repo_root.expanduser().resolve()
    backup_base = Path(backup_root or root / "output" / "_kalyanaram_refresh_backups")
    backup_base = _require_under_root(backup_base, root, label="backup_root")
    backup_dir, backups = _backup_targets(prepared, backup_root=backup_base)
    staged: dict[str, Path] = {}
    try:
        _assert_pre_state(prepared.plan)
        write_items = {item["role"]: item for item in prepared.plan["writes"]}
        for role, payload in prepared.payloads.items():
            staged[role] = _write_staged_payload(
                prepared.paths[role],
                payload,
                str(write_items[role]["after"]["sha256"]),
            )
        _assert_pre_state(prepared.plan)
        for role, target in prepared.paths.items():
            os.replace(staged[role], target)
        _verify_post_state(prepared)
    except BaseException:
        for staged_path in staged.values():
            staged_path.unlink(missing_ok=True)
        _restore_backups(prepared, backups)
        manifest_path = backup_dir / "backup_manifest.json"
        manifest = _load_json(manifest_path, label="backup manifest")
        manifest["status"] = "rolled_back"
        atomic_write_json(str(manifest_path), manifest)
        raise

    manifest_path = backup_dir / "backup_manifest.json"
    manifest = _load_json(manifest_path, label="backup manifest")
    manifest["status"] = "committed"
    atomic_write_json(str(manifest_path), manifest)
    return {
        "status": "committed",
        "plan_hash": prepared.plan["plan_hash"],
        "backup_dir": str(backup_dir),
        "writes": [
            {
                "role": item["role"],
                "path": item["path"],
                "count": item["after"]["count"],
                "sha256": item["after"]["sha256"],
            }
            for item in prepared.plan["writes"]
        ],
        "catalog_ref_ids": prepared.plan["catalog_ref_ids"],
        "requires_post_refresh_reconcile": prepared.plan[
            "requires_post_refresh_reconcile"
        ],
    }


def _write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    target = path.expanduser().resolve()
    if target.exists():
        raise RefreshError(f"plan output already exists: {target}")
    atomic_write_json(str(target), dict(plan))
    if validate_plan(_load_json(target, label="written refresh plan")) != dict(plan):
        raise RefreshError("written refresh plan failed read-back validation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply the one-paper Kalyanaram Stage 1 supplemental refresh. "
            "Dry-run is the default; --apply requires an accepted plan file."
        )
    )
    parser.add_argument(
        "source_summary", help="canonical one-record Stage 1 summary JSON"
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--plan-out", help="optional path for the dry-run plan JSON")
    parser.add_argument(
        "--apply", action="store_true", help="apply an accepted dry-run plan"
    )
    parser.add_argument(
        "--expected-plan", help="accepted dry-run plan JSON; required with --apply"
    )
    parser.add_argument(
        "--backup-root", help="backup directory under the repository root"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths = RefreshPaths.from_repo_root(args.repo_root)
        if args.apply:
            if not args.expected_plan:
                raise RefreshError("--apply requires --expected-plan")
            if args.plan_out:
                raise RefreshError("--plan-out cannot be combined with --apply")
            expected_plan = _load_json(
                Path(args.expected_plan).expanduser().resolve(),
                label="accepted refresh plan",
            )
            if not isinstance(expected_plan, Mapping):
                raise RefreshError("accepted refresh plan must be a JSON object")
            result = apply_refresh(
                paths,
                args.source_summary,
                expected_plan,
                expected_pre_counts=DEFAULT_EXPECTED_PRE_COUNTS,
                backup_root=args.backup_root,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.expected_plan:
            raise RefreshError("--expected-plan is only valid with --apply")
        prepared = prepare_refresh(
            paths,
            args.source_summary,
            expected_pre_counts=DEFAULT_EXPECTED_PRE_COUNTS,
        )
        if args.plan_out:
            _write_plan(Path(args.plan_out), prepared.plan)
        print(json.dumps(prepared.plan, ensure_ascii=False, indent=2))
        return 0
    except (RefreshError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

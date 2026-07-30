"""Prepare the frozen 84-paper PPH Stage 1 corpus without provider calls.

This module is deliberately limited to deterministic, local preparation:

* cross-check the frozen Zotero metadata, memberships, and eligibility manifest;
* cross-check every packaged PDF by path, byte size, and SHA-256;
* materialize one unambiguous PDF per eligible parent;
* write one parser-round-trippable Zotero report and seven exact-set manifests;
* derive a one-use config and a parent :class:`RuntimeJobSpec` draft.

It never invokes ``AgentRuntimeRunner`` or any model/provider surface.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from file_finder import create_file_index, resolve_pdf_match  # noqa: E402
from runtime.job_spec import (  # noqa: E402
    RuntimeJobSpec,
    RuntimeSourceSpec,
    save_runtime_job_spec,
)
from runtime.source_intake import build_zotero_source_bundle  # noqa: E402
from services.paper_identity import (  # noqa: E402
    build_canonical_paper_key,
    normalize_doi,
    normalized_title_key,
)
from zotero_parser import parse_zotero_report_result  # noqa: E402


DEFAULT_SOURCE_CONFIG = REPO_ROOT / "config.ini"
DEFAULT_QUEUE_BASENAME = "_queue/queue.json"

CORPUS_SIZE = 84
FROZEN_PARENT_COUNT = 88
EXCLUDED_COUNT = 4
RUNTIME_OUTPUT_NAME = "runtime_output_56570"
DERIVED_CONFIG_NAME = "runtime_config_56570.ini"
SELECTED_LIBRARY_NAME = "selected_library"
SELECTED_MANIFEST_NAME = "selected_sources_manifest.json"
ZOTERO_REPORT_NAME = "zotero_report.txt"
TOPIC_DIRECTORY_NAME = "topic_selections"
PARENT_SPEC_NAME = "parent_runtime_job_spec.json"
BUNDLE_MANIFEST_NAME = "rebuild_bundle_manifest.json"

KALYANARAM_CANONICAL_KEY = "10.1287/mksc.14.3.g161"
KALYANARAM_SOURCE_PDF_SHA256 = (
    "2ec00e6240bb8309b2901a542df62a55a81ff5d5efcd43ef8ae0997b5b36c1d5"
)
KALYANARAM_SUMMARY_SHA256 = (
    "7a887097fd63b31b45b58989328123ba13fbfbe8c313a718d88837cb62650796"
)
DEFAULT_KALYANARAM_SUMMARY = (
    REPO_ROOT
    / "output"
    / "pph_supplemental_kalyanaram_reference_price__20260729_102830_a8e1f2b4"
    / "artifacts"
    / "pph_supplemental_kalyanaram_reference_price_summaries.json"
)

TOPIC_SPECS: tuple[tuple[str, str, int], ...] = (
    ("S01", "01_综述_动态定价与价格劣势", 19),
    ("S02", "02_综述_平台既往让利与补贴", 21),
    ("S03", "03_假设_既往让利到价格不公平感", 25),
    ("S04", "04_假设_价格不公平感到持续使用", 19),
    ("S05", "05_假设_商业模式主观知识调节", 15),
    ("S90", "90_范围_亲历与知晓", 6),
    ("S91", "91_范围_适用边界与伦理", 7),
)

SELECTED_SCHEMA = "pph-stage1-selected-sources-v1"
TOPIC_SCHEMA = "pph-stage1-exact-set-v1"
BUNDLE_SCHEMA = "pph-stage1-rebuild-bundle-v1"


class Stage1RebuildError(RuntimeError):
    """Raised when the frozen rebuild contract cannot be proven."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Stage1RebuildError(f"cannot read JSON input: {path.name}") from exc


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [
                {str(key): str(value or "").strip() for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise Stage1RebuildError(f"cannot read CSV input: {path.name}") from exc


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parse_bool(value: Any, *, field: str) -> bool:
    normalized = str(value or "").strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise Stage1RebuildError(f"{field} must be an explicit true/false value")


def _parse_int(value: Any, *, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise Stage1RebuildError(f"{field} must be an integer") from exc


def _split_values(value: Any, separator: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in re.split(separator, str(value or "").strip())
        if item.strip()
    )


def _identity_key(title: Any, doi: Any) -> tuple[str, str]:
    title_key = normalized_title_key(title)
    if title_key == "unknown_title":
        raise Stage1RebuildError("paper identity is missing a usable title")
    raw_doi = str(doi or "").strip()
    normalized_doi = normalize_doi(raw_doi)
    if raw_doi and not normalized_doi:
        raise Stage1RebuildError("paper identity contains an invalid DOI")
    return normalized_doi, title_key


def _unique_index(
    values: Iterable[Mapping[str, Any]],
    *,
    key,
    label: str,
) -> dict[Any, Mapping[str, Any]]:
    result: dict[Any, Mapping[str, Any]] = {}
    for value in values:
        identity = key(value)
        if identity in result:
            raise Stage1RebuildError(f"{label} contains a duplicate identity")
        result[identity] = value
    return result


def _authors_from_parent(parent_data: Mapping[str, Any]) -> list[str]:
    authors: list[str] = []
    for creator in parent_data.get("creators") or []:
        if not isinstance(creator, Mapping):
            continue
        if str(creator.get("creatorType") or "").casefold() not in {
            "author",
            "bookauthor",
        }:
            continue
        literal_name = str(creator.get("name") or "").strip()
        first = str(creator.get("firstName") or "").strip()
        last = str(creator.get("lastName") or "").strip()
        name = literal_name or " ".join(item for item in (first, last) if item)
        if name:
            authors.append(name)
    return authors


def _year_from_parent(parent_data: Mapping[str, Any]) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(parent_data.get("date") or ""))
    return match.group(0) if match else ""


def _relative_under(root: Path, value: str | Path, *, label: str) -> Path:
    candidate = (root / Path(value)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise Stage1RebuildError(f"{label} escapes its declared root") from exc
    return candidate


def _validate_package_filename(filename: str) -> str:
    name = str(filename or "").strip()
    if not name or Path(name).name != name or Path(name).suffix.casefold() != ".pdf":
        raise Stage1RebuildError("package manifest contains an invalid PDF filename")
    return name


def _validate_frozen_inputs(
    *,
    closure_dir: Path,
    package_dir: Path,
) -> tuple[
    list[dict[str, Any]],
    dict[str, tuple[str, ...]],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    metadata_path = closure_dir / "01_live_zotero_metadata_snapshot.json"
    memberships_path = closure_dir / "02_section_memberships.csv"
    eligibility_path = closure_dir / "05_eligibility_manifest.csv"
    closure_manifest_path = closure_dir / "15_final_closure_manifest.json"
    closure_sha_path = closure_dir / "16_final_closure_manifest.sha256"
    package_manifest_path = package_dir / "_文件清单.csv"
    package_summary_path = package_dir / "_打包摘要.json"
    required = (
        metadata_path,
        memberships_path,
        eligibility_path,
        closure_manifest_path,
        closure_sha_path,
        package_manifest_path,
        package_summary_path,
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise Stage1RebuildError(
            "required frozen inputs are missing: " + ", ".join(sorted(missing))
        )

    metadata = _read_json(metadata_path)
    closure_manifest = _read_json(closure_manifest_path)
    package_summary = _read_json(package_summary_path)
    if (
        not isinstance(metadata, Mapping)
        or not isinstance(closure_manifest, Mapping)
        or not isinstance(package_summary, Mapping)
    ):
        raise Stage1RebuildError("frozen JSON inputs must be objects")
    try:
        closure_sha_text = closure_sha_path.read_text(
            encoding="utf-8-sig"
        ).strip()
    except (OSError, UnicodeError) as exc:
        raise Stage1RebuildError("cannot read closure SHA-256 sidecar") from exc
    closure_sha_match = re.fullmatch(
        r"([0-9a-fA-F]{64})\s+15_final_closure_manifest\.json",
        closure_sha_text,
    )
    if (
        closure_sha_match is None
        or closure_sha_match.group(1).casefold()
        != file_sha256(closure_manifest_path)
    ):
        raise Stage1RebuildError("final closure manifest SHA-256 chain is broken")
    acceptance_counts = closure_manifest.get("acceptance_counts")
    if (
        closure_manifest.get("closure_id") != "acceptance_closure_20260728"
        or not isinstance(acceptance_counts, Mapping)
        or int(acceptance_counts.get("parents") or 0) != FROZEN_PARENT_COUNT
        or int(acceptance_counts.get("eligible") or 0) != CORPUS_SIZE
        or int(acceptance_counts.get("excluded") or 0) != EXCLUDED_COUNT
    ):
        raise Stage1RebuildError("final closure manifest acceptance contract is invalid")
    closure_artifacts = closure_manifest.get("artifacts")
    if not isinstance(closure_artifacts, list):
        raise Stage1RebuildError("final closure manifest artifact chain is missing")
    closure_artifact_paths: set[str] = set()
    for artifact in closure_artifacts:
        if not isinstance(artifact, Mapping):
            raise Stage1RebuildError("final closure manifest artifact is invalid")
        relative_path = str(artifact.get("relative_path") or "")
        artifact_path = _relative_under(
            closure_dir,
            relative_path,
            label="closure artifact path",
        )
        if (
            not artifact_path.is_file()
            or artifact_path.stat().st_size
            != _parse_int(artifact.get("size_bytes"), field="size_bytes")
            or file_sha256(artifact_path)
            != str(artifact.get("sha256") or "").casefold()
        ):
            raise Stage1RebuildError(
                "final closure manifest artifact hash chain is broken"
            )
        closure_artifact_paths.add(relative_path)
    if not {
        metadata_path.name,
        memberships_path.name,
        eligibility_path.name,
    }.issubset(closure_artifact_paths):
        raise Stage1RebuildError(
            "final closure manifest does not bind the required 01/02/05 inputs"
        )

    parents = metadata.get("parents")
    child_groups = metadata.get("children_by_parent")
    if not isinstance(parents, list) or len(parents) != FROZEN_PARENT_COUNT:
        raise Stage1RebuildError("live metadata snapshot must contain exactly 88 parents")
    if not isinstance(child_groups, list) or len(child_groups) != FROZEN_PARENT_COUNT:
        raise Stage1RebuildError(
            "live metadata snapshot must contain one child group per parent"
        )

    parent_index: dict[str, Mapping[str, Any]] = {}
    parent_identity_index: dict[tuple[str, str], str] = {}
    for raw_parent in parents:
        if not isinstance(raw_parent, Mapping):
            raise Stage1RebuildError("live metadata parent entry is not an object")
        data = raw_parent.get("data")
        if not isinstance(data, Mapping):
            raise Stage1RebuildError("live metadata parent is missing data")
        parent_key = str(raw_parent.get("key") or data.get("key") or "").strip()
        if not parent_key or str(data.get("key") or parent_key).strip() != parent_key:
            raise Stage1RebuildError("live metadata parent key is inconsistent")
        if parent_key in parent_index:
            raise Stage1RebuildError("live metadata contains a duplicate parent key")
        identity = _identity_key(data.get("title"), data.get("DOI"))
        if identity in parent_identity_index:
            raise Stage1RebuildError(
                "live metadata contains a duplicate DOI plus normalized-title identity"
            )
        parent_index[parent_key] = raw_parent
        parent_identity_index[identity] = parent_key

    child_keys_by_parent: dict[str, tuple[str, ...]] = {}
    for raw_group in child_groups:
        if not isinstance(raw_group, Mapping):
            raise Stage1RebuildError("live metadata child group is not an object")
        parent_key = str(raw_group.get("parent_key") or "").strip()
        if parent_key not in parent_index or parent_key in child_keys_by_parent:
            raise Stage1RebuildError("live metadata child group parent is invalid")
        pdf_keys: list[str] = []
        for child in raw_group.get("children") or []:
            if not isinstance(child, Mapping):
                continue
            data = child.get("data")
            if not isinstance(data, Mapping):
                continue
            if str(data.get("contentType") or "").casefold() != "application/pdf":
                continue
            child_key = str(child.get("key") or data.get("key") or "").strip()
            if not child_key or str(data.get("parentItem") or "").strip() != parent_key:
                raise Stage1RebuildError("live PDF attachment identity is inconsistent")
            if child_key in pdf_keys:
                raise Stage1RebuildError("live metadata contains a duplicate child key")
            pdf_keys.append(child_key)
        child_keys_by_parent[parent_key] = tuple(pdf_keys)

    memberships = _read_csv(memberships_path)
    membership_by_parent: dict[str, list[Mapping[str, str]]] = {
        key: [] for key in parent_index
    }
    collection_name_to_key: dict[str, str] = {}
    collection_key_to_name: dict[str, str] = {}
    membership_pairs: set[tuple[str, str]] = set()
    for row in memberships:
        parent_key = row.get("paper_id", "")
        if parent_key not in parent_index:
            raise Stage1RebuildError("membership references an unknown parent")
        if not _parse_bool(row.get("readback_verified"), field="readback_verified"):
            raise Stage1RebuildError("membership is not live-readback verified")
        if row.get("resolved_zotero_key") != parent_key:
            raise Stage1RebuildError("membership resolved key does not match its parent")
        name = row.get("collection_name", "")
        key = row.get("collection_key", "")
        if not name or not key:
            raise Stage1RebuildError("membership is missing collection identity")
        if (parent_key, name) in membership_pairs:
            raise Stage1RebuildError("membership table contains a duplicate membership")
        membership_pairs.add((parent_key, name))
        if name in collection_name_to_key and collection_name_to_key[name] != key:
            raise Stage1RebuildError("collection name maps to multiple keys")
        if key in collection_key_to_name and collection_key_to_name[key] != name:
            raise Stage1RebuildError("collection key maps to multiple names")
        collection_name_to_key[name] = key
        collection_key_to_name[key] = name
        membership_by_parent[parent_key].append(row)

    expected_topic_names = {name for _topic_id, name, _count in TOPIC_SPECS}
    if not expected_topic_names.issubset(collection_name_to_key):
        raise Stage1RebuildError("frozen memberships are missing a required topic")

    eligibility_rows = _read_csv(eligibility_path)
    if len(eligibility_rows) != FROZEN_PARENT_COUNT:
        raise Stage1RebuildError("eligibility manifest must contain exactly 88 rows")
    eligibility_by_parent = _unique_index(
        eligibility_rows,
        key=lambda row: str(row.get("paper_id") or ""),
        label="eligibility manifest",
    )
    if set(eligibility_by_parent) != set(parent_index):
        raise Stage1RebuildError("eligibility parents do not equal live Zotero parents")

    eligible_rows = [
        dict(row)
        for row in eligibility_rows
        if str(row.get("eligibility") or "").casefold() == "eligible"
    ]
    excluded_rows = [
        row
        for row in eligibility_rows
        if str(row.get("eligibility") or "").casefold() != "eligible"
    ]
    if len(eligible_rows) != CORPUS_SIZE or len(excluded_rows) != EXCLUDED_COUNT:
        raise Stage1RebuildError("eligibility split must be exactly 84 eligible and 4 excluded")

    for row in eligibility_rows:
        parent_key = row["paper_id"]
        parent_data = parent_index[parent_key]["data"]
        if _identity_key(row.get("title"), row.get("doi")) != _identity_key(
            parent_data.get("title"),
            parent_data.get("DOI"),
        ):
            raise Stage1RebuildError(
                "eligibility identity does not match live Zotero metadata"
            )
        if row.get("zotero_key") != parent_key:
            raise Stage1RebuildError("eligibility Zotero key does not match paper_id")
        if not _parse_bool(
            row.get("live_readback_verified"),
            field="live_readback_verified",
        ):
            raise Stage1RebuildError("eligibility row is not live-readback verified")
        declared_collections = set(_split_values(row.get("collections"), r"\s*;\s*"))
        membership_collections = {
            membership["collection_name"]
            for membership in membership_by_parent[parent_key]
        }
        if declared_collections != membership_collections:
            raise Stage1RebuildError(
                "eligibility collections do not match frozen memberships"
            )
        attachment_keys = _split_values(
            row.get("pdf_attachment_keys"),
            r"\s*;\s*",
        )
        attachment_count = _parse_int(
            row.get("pdf_attachment_count"),
            field="pdf_attachment_count",
        )
        if (
            attachment_count != len(attachment_keys)
            or set(attachment_keys) != set(child_keys_by_parent[parent_key])
        ):
            raise Stage1RebuildError(
                "eligibility PDF attachments do not match live Zotero children"
            )
        if str(row.get("eligibility") or "").casefold() == "eligible":
            if not _parse_bool(row.get("has_pdf"), field="has_pdf"):
                raise Stage1RebuildError("eligible parent is missing a PDF")
            if not str(row.get("pdf_path") or "").strip():
                raise Stage1RebuildError("eligible parent is missing PDF provenance")

    package_rows = _read_csv(package_manifest_path)
    if len(package_rows) != FROZEN_PARENT_COUNT:
        raise Stage1RebuildError("package manifest must contain exactly 88 rows")
    package_index = _unique_index(
        package_rows,
        key=lambda row: _identity_key(row.get("题名"), row.get("DOI")),
        label="package manifest",
    )
    if set(package_index) != set(parent_identity_index):
        raise Stage1RebuildError(
            "package identities do not equal the frozen live Zotero identities"
        )

    if (
        _parse_int(
            package_summary.get("unique_parent_items"),
            field="unique_parent_items",
        )
        != FROZEN_PARENT_COUNT
        or _parse_int(
            package_summary.get("packaged_pdfs"),
            field="packaged_pdfs",
        )
        != FROZEN_PARENT_COUNT
        or _parse_int(
            package_summary.get("missing_pdfs"),
            field="missing_pdfs",
        )
        != 0
    ):
        raise Stage1RebuildError("package summary does not prove an 88-of-88 package")

    pdf_root = (package_dir / "PDF").resolve()
    if not pdf_root.is_dir():
        raise Stage1RebuildError("package PDF directory is missing")
    seen_filenames: set[str] = set()
    selected: list[dict[str, Any]] = []
    for package_row in package_rows:
        sequence = _parse_int(package_row.get("序号"), field="序号")
        filename = _validate_package_filename(package_row.get("文件名", ""))
        filename_key = filename.casefold()
        if filename_key in seen_filenames:
            raise Stage1RebuildError("package manifest contains a duplicate filename")
        seen_filenames.add(filename_key)
        source_pdf = _relative_under(pdf_root, filename, label="package PDF path")
        if not source_pdf.is_file() or source_pdf.is_symlink():
            raise Stage1RebuildError("package PDF path is missing or not a regular file")
        expected_size = _parse_int(package_row.get("字节数"), field="字节数")
        expected_hash = str(package_row.get("SHA256") or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise Stage1RebuildError("package manifest contains an invalid SHA-256")
        if source_pdf.stat().st_size != expected_size:
            raise Stage1RebuildError("package PDF byte size does not match its manifest")
        if file_sha256(source_pdf) != expected_hash:
            raise Stage1RebuildError("package PDF SHA-256 does not match its manifest")

        identity = _identity_key(package_row.get("题名"), package_row.get("DOI"))
        parent_key = parent_identity_index[identity]
        eligibility = eligibility_by_parent[parent_key]
        declared_package_collections = set(
            _split_values(package_row.get("所属集合"), r"\s*\|\s*")
        )
        frozen_collections = {
            row["collection_name"] for row in membership_by_parent[parent_key]
        }
        if declared_package_collections != frozen_collections:
            raise Stage1RebuildError(
                "package collections do not match frozen memberships"
            )
        if str(eligibility.get("eligibility") or "").casefold() != "eligible":
            continue

        parent_data = parent_index[parent_key]["data"]
        authors = _authors_from_parent(parent_data)
        year = _year_from_parent(parent_data)
        paper_for_identity = {
            "title": str(parent_data.get("title") or "").strip(),
            "authors": authors,
            "year": year,
            "doi": normalize_doi(parent_data.get("DOI")),
        }
        canonical_key = build_canonical_paper_key(paper_for_identity)
        if not canonical_key or canonical_key.startswith("source:"):
            raise Stage1RebuildError("eligible parent lacks a stable canonical paper key")
        selected.append(
            {
                "source_order": sequence,
                "paper_id": parent_key,
                "zotero_parent_key": parent_key,
                "canonical_paper_key": canonical_key,
                "title": paper_for_identity["title"],
                "normalized_title": identity[1],
                "doi": paper_for_identity["doi"],
                "authors": authors,
                "year": year,
                "journal": str(
                    parent_data.get("publicationTitle")
                    or parent_data.get("proceedingsTitle")
                    or ""
                ).strip(),
                "parent_version": _parse_int(
                    parent_data.get("version", 0),
                    field="parent_version",
                ),
                "collections": sorted(frozen_collections),
                "pdf_attachment_count": _parse_int(
                    eligibility.get("pdf_attachment_count"),
                    field="pdf_attachment_count",
                ),
                "pdf_attachment_keys": list(
                    _split_values(
                        eligibility.get("pdf_attachment_keys"),
                        r"\s*;\s*",
                    )
                ),
                "package_source": str(package_row.get("来源") or "").strip(),
                "package_pdf_path": str(source_pdf),
                "selected_pdf_path": f"{SELECTED_LIBRARY_NAME}/{filename}",
                "pdf_filename": filename,
                "pdf_size_bytes": expected_size,
                "pdf_sha256": expected_hash,
            }
        )

    selected.sort(key=lambda row: int(row["source_order"]))
    if len(selected) != CORPUS_SIZE:
        raise Stage1RebuildError("selected corpus is not exactly 84 papers")
    orders = [int(row["source_order"]) for row in selected]
    if len(set(orders)) != CORPUS_SIZE:
        raise Stage1RebuildError("selected corpus has duplicate package order values")
    for field in ("paper_id", "canonical_paper_key", "pdf_sha256", "pdf_filename"):
        values = [
            str(row[field]).casefold() if field == "pdf_filename" else str(row[field])
            for row in selected
        ]
        if len(set(values)) != CORPUS_SIZE:
            raise Stage1RebuildError(f"selected corpus has duplicate {field} values")
    kalyanaram = [
        row
        for row in selected
        if row["canonical_paper_key"] == KALYANARAM_CANONICAL_KEY
    ]
    if (
        len(kalyanaram) != 1
        or kalyanaram[0]["pdf_sha256"] != KALYANARAM_SOURCE_PDF_SHA256
    ):
        raise Stage1RebuildError(
            "selected corpus does not contain the verified Kalyanaram source"
        )

    topic_memberships: dict[str, tuple[str, ...]] = {}
    union: set[str] = set()
    eligible_parent_keys = {str(row["paper_id"]) for row in selected}
    for topic_id, topic_name, expected_count in TOPIC_SPECS:
        paper_ids = {
            str(row["paper_id"])
            for row in memberships
            if str(row.get("collection_name") or "") == topic_name
            and str(row.get("paper_id") or "") in eligible_parent_keys
        }
        if len(paper_ids) != expected_count:
            raise Stage1RebuildError(
                f"{topic_id} exact-set count is {len(paper_ids)}, expected {expected_count}"
            )
        ordered = tuple(
            str(row["paper_id"])
            for row in selected
            if str(row["paper_id"]) in paper_ids
        )
        if len(ordered) != expected_count:
            raise Stage1RebuildError(f"{topic_id} exact-set ordering is inconsistent")
        topic_memberships[topic_id] = ordered
        union.update(ordered)
    if union != eligible_parent_keys:
        raise Stage1RebuildError("seven exact sets do not union to all 84 eligible papers")

    input_hashes = {
        path.name: file_sha256(path)
        for path in required
    }
    return (
        selected,
        topic_memberships,
        collection_name_to_key,
        input_hashes,
        {
            metadata_path.name: str(metadata_path.resolve()),
            memberships_path.name: str(memberships_path.resolve()),
            eligibility_path.name: str(eligibility_path.resolve()),
            closure_manifest_path.name: str(closure_manifest_path.resolve()),
            closure_sha_path.name: str(closure_sha_path.resolve()),
            package_manifest_path.name: str(package_manifest_path.resolve()),
            package_summary_path.name: str(package_summary_path.resolve()),
        },
    )


def _materialize_selected_library(
    selected: Sequence[Mapping[str, Any]],
    *,
    library_root: Path,
) -> dict[str, int]:
    library_root.mkdir(parents=True, exist_ok=False)
    hardlinks = 0
    copies = 0
    for row in selected:
        source = Path(str(row["package_pdf_path"])).resolve()
        destination = _relative_under(
            library_root,
            str(row["pdf_filename"]),
            label="selected PDF path",
        )
        try:
            os.link(source, destination)
            hardlinks += 1
        except OSError:
            shutil.copy2(source, destination)
            copies += 1
        if (
            destination.stat().st_size != int(row["pdf_size_bytes"])
            or file_sha256(destination) != str(row["pdf_sha256"])
        ):
            raise Stage1RebuildError("materialized PDF failed size/hash readback")
    actual = sorted(
        path.name.casefold()
        for path in library_root.iterdir()
        if path.is_file() and path.suffix.casefold() == ".pdf"
    )
    expected = sorted(str(row["pdf_filename"]).casefold() for row in selected)
    if actual != expected:
        raise Stage1RebuildError(
            "selected library does not contain exactly the 84 expected PDFs"
        )
    return {"hardlinks": hardlinks, "copies": copies}


def _zotero_report_text(selected: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "Zotero Report",
        "Generated deterministically from acceptance_closure_20260728",
    ]
    for row in selected:
        lines.extend(["*", f"Title\t{row['title']}"])
        for author in row.get("authors") or []:
            lines.append(f"Author\t{author}")
        if row.get("year"):
            lines.append(f"Year\t{row['year']}")
        if row.get("journal"):
            lines.append(f"Publication Title\t{row['journal']}")
        if row.get("doi"):
            lines.append(f"DOI\t{row['doi']}")
        lines.append("Source Identity Policy\tfrozen-source-sha256-v1")
        lines.append(f"Source PDF SHA256\t{row['pdf_sha256']}")
        lines.append(f"Attachment\t{row['pdf_filename']}")
    return "\n".join(lines) + "\n"


def _verify_report_round_trip(
    report_path: Path,
    library_root: Path,
    selected: Sequence[Mapping[str, Any]],
) -> None:
    parsed = parse_zotero_report_result(str(report_path))
    if (
        parsed.status != "ok"
        or len(parsed.records) != CORPUS_SIZE
        or parsed.stats.skipped_entries != 0
    ):
        raise Stage1RebuildError("generated Zotero report failed parser round-trip")
    expected_by_key = {
        str(row["canonical_paper_key"]): row for row in selected
    }
    parsed_keys = [build_canonical_paper_key(paper) for paper in parsed.papers]
    if len(set(parsed_keys)) != CORPUS_SIZE or set(parsed_keys) != set(expected_by_key):
        raise Stage1RebuildError(
            "generated Zotero report changed canonical paper identities"
        )
    index = create_file_index(str(library_root))
    if index.entry_count != CORPUS_SIZE:
        raise Stage1RebuildError("selected library PDF index is not exactly 84")
    for paper, canonical_key in zip(parsed.papers, parsed_keys):
        attachments = paper.get("attachments") or []
        if len(attachments) != 1:
            raise Stage1RebuildError(
                "generated Zotero entry does not contain exactly one attachment"
            )
        match = resolve_pdf_match(paper, str(library_root), index)
        if match.status != "matched" or not match.selected_path:
            raise Stage1RebuildError(
                "generated Zotero entry does not resolve to one unambiguous PDF"
            )
        expected = expected_by_key[canonical_key]
        if (
            Path(match.selected_path).name != expected["pdf_filename"]
            or file_sha256(match.selected_path) != expected["pdf_sha256"]
        ):
            raise Stage1RebuildError(
                "generated Zotero entry resolved to the wrong PDF"
            )


def _verify_runtime_source_bundle(
    report_path: Path,
    library_root: Path,
    selected: Sequence[Mapping[str, Any]],
) -> str:
    """Run the provider-free runtime intake and require 84 canonical work items."""

    try:
        source_bundle = build_zotero_source_bundle(
            project_name="pph_stage1_rebuild_preflight",
            zotero_report=str(report_path),
            library_path=str(library_root),
        )
    except (OSError, ValueError) as exc:
        raise Stage1RebuildError(
            "provider-free runtime source intake failed"
        ) from exc
    snapshot = source_bundle.source_snapshot
    if (
        len(source_bundle.paper_work_items) != CORPUS_SIZE
        or int(snapshot.get("matched_count") or 0) != CORPUS_SIZE
        or snapshot.get("missing_titles")
        or snapshot.get("ambiguous_matches")
        or snapshot.get("quarantined_sources")
        or snapshot.get("canonical_ready") is not True
    ):
        missing_count = len(snapshot.get("missing_titles") or [])
        ambiguous_count = len(snapshot.get("ambiguous_matches") or [])
        quarantined = list(snapshot.get("quarantined_sources") or [])
        quarantine_reasons = sorted(
            {
                str(reason)
                for item in quarantined
                if isinstance(item, Mapping)
                for reason in (item.get("reasons") or [])
            }
        )
        quarantine_details = [
            {
                "title": str(item.get("title") or ""),
                "reasons": list(item.get("reasons") or []),
                "expected_doi": str(
                    (item.get("expected") or {}).get("doi") or ""
                ),
                "observed_doi": str(
                    (item.get("observed") or {}).get("doi") or ""
                ),
            }
            for item in quarantined
            if isinstance(item, Mapping)
        ]
        raise Stage1RebuildError(
            "runtime source intake contract failed: "
            f"matched={len(source_bundle.paper_work_items)}, "
            f"missing={missing_count}, ambiguous={ambiguous_count}, "
            f"quarantined={len(quarantined)}, "
            f"quarantine_reasons={quarantine_reasons}, "
            f"quarantine_details={quarantine_details}"
        )
    expected = {
        str(row["canonical_paper_key"]): row
        for row in selected
    }
    actual_keys = [
        str(item.canonical_paper_key)
        for item in source_bundle.paper_work_items
    ]
    if len(set(actual_keys)) != CORPUS_SIZE or set(actual_keys) != set(expected):
        raise Stage1RebuildError(
            "runtime source intake changed the 84 canonical paper identities"
        )
    for item in source_bundle.paper_work_items:
        row = expected[item.canonical_paper_key]
        if (
            Path(item.source_pdf).name != row["pdf_filename"]
            or file_sha256(item.source_pdf) != row["pdf_sha256"]
        ):
            raise Stage1RebuildError(
                "runtime source intake selected a non-canonical PDF"
            )
    return source_bundle.fingerprint()


def _replace_config_fields(
    source_config: Path,
    destination: Path,
    *,
    runtime_output_root: Path,
) -> None:
    try:
        source_text = source_config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Stage1RebuildError("cannot read the source runtime config") from exc

    section = ""
    replaced = {
        ("paths", "output_path"): 0,
        ("performance", "max_workers"): 0,
        ("validation", "max_workers"): 0,
    }
    output_lines: list[str] = []
    for line in source_text.splitlines(keepends=True):
        section_match = re.match(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?(?:\r?\n)?$", line)
        if section_match:
            section = section_match.group(1).strip().casefold()
            output_lines.append(line)
            continue
        key_match = re.match(
            r"^(?P<indent>\s*)(?P<key>[^#;=\s][^=]*?)(?P<sep>\s*=\s*)(?P<value>.*?)(?P<newline>\r?\n)?$",
            line,
        )
        if not key_match:
            output_lines.append(line)
            continue
        key = key_match.group("key").strip().casefold()
        identity = (section, key)
        if identity not in replaced:
            output_lines.append(line)
            continue
        replacement = (
            str(runtime_output_root.resolve())
            if identity == ("paths", "output_path")
            else "1"
        )
        output_lines.append(
            f"{key_match.group('indent')}{key_match.group('key')}"
            f"{key_match.group('sep')}{replacement}{key_match.group('newline') or ''}"
        )
        replaced[identity] += 1

    if any(count != 1 for count in replaced.values()):
        raise Stage1RebuildError(
            "source config must define exactly one Paths.output_path and "
            "one max_workers option in Performance and Validation"
        )
    destination.write_text("".join(output_lines), encoding="utf-8")

    source_parser = configparser.RawConfigParser(interpolation=None)
    derived_parser = configparser.RawConfigParser(interpolation=None)
    try:
        source_parser.read(source_config, encoding="utf-8")
        derived_parser.read(destination, encoding="utf-8")
    except configparser.Error as exc:
        raise Stage1RebuildError("runtime config failed structural readback") from exc
    if source_parser.sections() != derived_parser.sections():
        raise Stage1RebuildError("derived config changed the section structure")
    for current_section in source_parser.sections():
        if set(source_parser.options(current_section)) != set(
            derived_parser.options(current_section)
        ):
            raise Stage1RebuildError("derived config changed the option structure")
        for option in source_parser.options(current_section):
            identity = (current_section.casefold(), option.casefold())
            if identity in replaced:
                continue
            if source_parser.get(current_section, option, raw=True) != derived_parser.get(
                current_section,
                option,
                raw=True,
            ):
                raise Stage1RebuildError(
                    "derived config changed a non-authorized option"
                )
    if (
        Path(derived_parser.get("Paths", "output_path", raw=True)).resolve()
        != runtime_output_root.resolve()
        or derived_parser.getint("Performance", "max_workers") != 1
        or derived_parser.getint("Validation", "max_workers") != 1
    ):
        raise Stage1RebuildError("derived config did not persist the two required changes")


def _assert_pristine_runtime_output(runtime_output_root: Path) -> None:
    if not runtime_output_root.is_dir():
        raise Stage1RebuildError("runtime output root does not exist")
    entries = list(runtime_output_root.iterdir())
    if entries:
        raise Stage1RebuildError("runtime output root is not pristine and empty")
    if list(runtime_output_root.rglob("*_summaries.json")):
        raise Stage1RebuildError("runtime output root contains a stale Stage 1 summary")


def _validate_kalyanaram_summary(path: Path) -> None:
    if not path.is_file() or file_sha256(path) != KALYANARAM_SUMMARY_SHA256:
        raise Stage1RebuildError(
            "canonical Kalyanaram reuse summary is missing or hash-mismatched"
        )
    payload = _read_json(path)
    if not isinstance(payload, list) or len(payload) != 1:
        raise Stage1RebuildError(
            "canonical Kalyanaram reuse summary must contain exactly one record"
        )
    record = payload[0]
    if not isinstance(record, Mapping):
        raise Stage1RebuildError("canonical Kalyanaram reuse summary is invalid")
    paper_info = record.get("paper_info")
    if not isinstance(paper_info, Mapping):
        raise Stage1RebuildError(
            "canonical Kalyanaram reuse summary lacks paper_info"
        )
    if build_canonical_paper_key(paper_info) != KALYANARAM_CANONICAL_KEY:
        raise Stage1RebuildError(
            "canonical Kalyanaram reuse summary has the wrong identity"
        )


def _write_parent_spec(
    *,
    physical_bundle: Path,
    logical_bundle: Path,
    target: Path,
    project_name: str,
    canonical_summary: Path,
) -> None:
    selected_manifest = physical_bundle / SELECTED_MANIFEST_NAME
    spec = RuntimeJobSpec(
        project_name=project_name,
        source=RuntimeSourceSpec(
            mode="zotero",
            zotero_report=str(logical_bundle / ZOTERO_REPORT_NAME),
            library_path=str(logical_bundle / SELECTED_LIBRARY_NAME),
        ),
        config=str(logical_bundle / DERIVED_CONFIG_NAME),
        action="analyze",
        reuse_stage1=True,
        reuse_summary_files=(str(canonical_summary),),
        queue_file=str(
            logical_bundle / RUNTIME_OUTPUT_NAME / DEFAULT_QUEUE_BASENAME
        ),
        keep_legacy_projections=False,
        metadata={
            "requested_stages": ["source_intake", "analyze"],
            "validation_required": False,
            "require_clean_validation": False,
            "allow_unvalidated_when_validation_optional": True,
            "stage1_rebuild_contract": "pph-84-exact-set-v1",
            "expected_corpus_count": CORPUS_SIZE,
            "selected_source_manifest": str(
                logical_bundle / SELECTED_MANIFEST_NAME
            ),
            "selected_source_manifest_sha256": file_sha256(selected_manifest),
            "canonical_kalyanaram_summary_sha256": KALYANARAM_SUMMARY_SHA256,
            "prep_provider_executed": False,
            "user_authorized_execution": True,
        },
    )
    spec.validate()
    save_runtime_job_spec(target, spec)


def build_parent_spec(
    bundle_dir: str | Path,
    *,
    project_name: str = "pph_master_stage1_rebuild_56570",
    output_path: str | Path | None = None,
    kalyanaram_summary: str | Path = DEFAULT_KALYANARAM_SUMMARY,
) -> dict[str, Any]:
    bundle = Path(bundle_dir).expanduser().resolve()
    selected_manifest = bundle / SELECTED_MANIFEST_NAME
    report = bundle / ZOTERO_REPORT_NAME
    library = bundle / SELECTED_LIBRARY_NAME
    derived_config = bundle / DERIVED_CONFIG_NAME
    runtime_output_root = bundle / RUNTIME_OUTPUT_NAME
    for required in (selected_manifest, report, derived_config):
        if not required.is_file():
            raise Stage1RebuildError(
                f"rebuild bundle is missing required artifact: {required.name}"
            )
    if not library.is_dir():
        raise Stage1RebuildError("rebuild bundle is missing the selected library")
    _assert_pristine_runtime_output(runtime_output_root)
    audit = audit_bundle(bundle, require_pristine_runtime=True)

    canonical_summary = Path(kalyanaram_summary).expanduser().resolve()
    _validate_kalyanaram_summary(canonical_summary)
    target = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else bundle / PARENT_SPEC_NAME
    )
    if target.parent != bundle and output_path is None:
        raise Stage1RebuildError("default parent spec path escaped the bundle")
    _write_parent_spec(
        physical_bundle=bundle,
        logical_bundle=bundle,
        target=target,
        project_name=project_name,
        canonical_summary=canonical_summary,
    )
    persisted = json.loads(target.read_text(encoding="utf-8"))
    reuse_files = persisted.get("reuse_summary_files")
    if persisted.get("reuse_stage1") is not True or reuse_files != [
        str(canonical_summary)
    ]:
        raise Stage1RebuildError(
            "parent spec did not preserve the single-summary reuse contract"
        )
    return {
        "status": "ready",
        "provider_executed": False,
        "parent_spec_path": str(target),
        "parent_spec_sha256": file_sha256(target),
        "runtime_output_root": str(runtime_output_root),
        "runtime_output_empty": True,
        "reuse_summary_count": 1,
        "reuse_summary_sha256": KALYANARAM_SUMMARY_SHA256,
        "bundle_audit": audit,
    }


def _build_into_staging(
    *,
    staging: Path,
    final_bundle: Path,
    selected: Sequence[Mapping[str, Any]],
    topic_memberships: Mapping[str, tuple[str, ...]],
    collection_name_to_key: Mapping[str, str],
    input_hashes: Mapping[str, str],
    input_paths: Mapping[str, str],
    source_config: Path,
    kalyanaram_summary: Path,
) -> dict[str, Any]:
    library = staging / SELECTED_LIBRARY_NAME
    materialization = _materialize_selected_library(selected, library_root=library)
    report = staging / ZOTERO_REPORT_NAME
    report.write_text(_zotero_report_text(selected), encoding="utf-8")
    _verify_report_round_trip(report, library, selected)
    source_bundle_fingerprint = _verify_runtime_source_bundle(
        report,
        library,
        selected,
    )

    runtime_output_root = staging / RUNTIME_OUTPUT_NAME
    runtime_output_root.mkdir()
    _assert_pristine_runtime_output(runtime_output_root)
    derived_config = staging / DERIVED_CONFIG_NAME
    _replace_config_fields(
        source_config,
        derived_config,
        runtime_output_root=final_bundle / RUNTIME_OUTPUT_NAME,
    )

    corpus_hash = _canonical_hash(
        {
            "ordered_sources": [
                {
                    "canonical_paper_key": row["canonical_paper_key"],
                    "paper_id": row["paper_id"],
                    "pdf_sha256": row["pdf_sha256"],
                }
                for row in selected
            ]
        }
    )
    selected_manifest_payload = {
        "artifact_type": "selected_source_manifest",
        "schema_version": SELECTED_SCHEMA,
        "corpus_count": CORPUS_SIZE,
        "excluded_count": EXCLUDED_COUNT,
        "corpus_hash": corpus_hash,
        "source_inputs": {
            name: {"path": input_paths[file_name], "sha256": input_hashes[file_name]}
            for name, file_name in (
                ("live_zotero_metadata", "01_live_zotero_metadata_snapshot.json"),
                ("section_memberships", "02_section_memberships.csv"),
                ("eligibility_manifest", "05_eligibility_manifest.csv"),
                ("final_closure_manifest", "15_final_closure_manifest.json"),
                ("final_closure_sha256", "16_final_closure_manifest.sha256"),
                ("package_manifest", "_文件清单.csv"),
                ("package_summary", "_打包摘要.json"),
            )
        },
        "zotero_report": {
            "path": ZOTERO_REPORT_NAME,
            "sha256": file_sha256(report),
            "parser_version": "zotero-parser-v1",
            "record_count": CORPUS_SIZE,
        },
        "runtime_source_intake": {
            "canonical_ready": True,
            "matched_count": CORPUS_SIZE,
            "missing_count": 0,
            "ambiguous_count": 0,
            "quarantined_count": 0,
            "source_bundle_fingerprint": source_bundle_fingerprint,
            "provider_executed": False,
        },
        "selected_library": {
            "path": SELECTED_LIBRARY_NAME,
            "pdf_count": CORPUS_SIZE,
            "materialization": materialization,
        },
        "selected_sources": list(selected),
    }
    selected_manifest = staging / SELECTED_MANIFEST_NAME
    _write_json(selected_manifest, selected_manifest_payload)
    selected_manifest_hash = file_sha256(selected_manifest)

    selected_by_id = {str(row["paper_id"]): row for row in selected}
    topic_dir = staging / TOPIC_DIRECTORY_NAME
    topic_dir.mkdir()
    topic_artifacts: list[dict[str, Any]] = []
    topic_union: set[str] = set()
    for topic_id, topic_name, expected_count in TOPIC_SPECS:
        ordered_parent_keys = topic_memberships[topic_id]
        ordered_paper_keys = [
            str(selected_by_id[parent_key]["canonical_paper_key"])
            for parent_key in ordered_parent_keys
        ]
        topic_payload: dict[str, Any] = {
            "artifact_type": "stage1_exact_set_selection",
            "schema_version": TOPIC_SCHEMA,
            "topic_id": topic_id,
            "collection_name": topic_name,
            "collection_key": collection_name_to_key[topic_name],
            "expected_count": expected_count,
            "source_manifest_path": f"../{SELECTED_MANIFEST_NAME}",
            "source_manifest_sha256": selected_manifest_hash,
            "ordered_paper_keys": ordered_paper_keys,
            "ordered_zotero_parent_keys": list(ordered_parent_keys),
        }
        topic_payload["selection_hash"] = _canonical_hash(topic_payload)
        topic_path = topic_dir / f"{topic_id}_selection.json"
        _write_json(topic_path, topic_payload)
        topic_artifacts.append(
            {
                "topic_id": topic_id,
                "path": f"{TOPIC_DIRECTORY_NAME}/{topic_path.name}",
                "sha256": file_sha256(topic_path),
                "expected_count": expected_count,
                "selection_hash": topic_payload["selection_hash"],
            }
        )
        topic_union.update(ordered_parent_keys)
    if topic_union != set(selected_by_id):
        raise Stage1RebuildError("written topic selections do not union to 84 papers")

    _validate_kalyanaram_summary(kalyanaram_summary)
    _write_parent_spec(
        physical_bundle=staging,
        logical_bundle=final_bundle,
        target=staging / PARENT_SPEC_NAME,
        project_name="pph_master_stage1_rebuild_56570",
        canonical_summary=kalyanaram_summary,
    )

    bundle_manifest = {
        "artifact_type": "stage1_rebuild_bundle",
        "schema_version": BUNDLE_SCHEMA,
        "provider_executed": False,
        "corpus_count": CORPUS_SIZE,
        "topic_count": len(TOPIC_SPECS),
        "topic_union_count": len(topic_union),
        "runtime_contract": {
            "runtime_output_path": RUNTIME_OUTPUT_NAME,
            "runtime_output_must_be_empty": True,
            "derived_config_path": DERIVED_CONFIG_NAME,
            "derived_config_sha256": file_sha256(derived_config),
            "authorized_config_changes": [
                "Paths.output_path",
                "Performance.max_workers",
                "Validation.max_workers",
            ],
            "max_workers": 1,
            "parent_spec_path": PARENT_SPEC_NAME,
            "parent_spec_sha256": file_sha256(staging / PARENT_SPEC_NAME),
            "reuse_stage1": True,
            "reuse_summary_count": 1,
            "reuse_summary_sha256": KALYANARAM_SUMMARY_SHA256,
        },
        "artifacts": [
            {
                "path": SELECTED_MANIFEST_NAME,
                "sha256": selected_manifest_hash,
            },
            {
                "path": ZOTERO_REPORT_NAME,
                "sha256": file_sha256(report),
            },
            *topic_artifacts,
        ],
    }
    _write_json(staging / BUNDLE_MANIFEST_NAME, bundle_manifest)
    return {
        "corpus_hash": corpus_hash,
        "materialization": materialization,
        "parent_spec_sha256": file_sha256(staging / PARENT_SPEC_NAME),
    }


def build_rebuild_bundle(
    *,
    closure_dir: str | Path,
    package_dir: str | Path,
    output_dir: str | Path,
    source_config: str | Path = DEFAULT_SOURCE_CONFIG,
    kalyanaram_summary: str | Path = DEFAULT_KALYANARAM_SUMMARY,
) -> dict[str, Any]:
    closure = Path(closure_dir).expanduser().resolve()
    package = Path(package_dir).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve()
    config = Path(source_config).expanduser().resolve()
    canonical_summary = Path(kalyanaram_summary).expanduser().resolve()
    if not closure.is_dir() or not package.is_dir():
        raise Stage1RebuildError("closure and package inputs must be directories")
    if not config.is_file():
        raise Stage1RebuildError("source runtime config is missing")
    if target.exists():
        raise Stage1RebuildError("output directory already exists; refusing overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)

    (
        selected,
        topic_memberships,
        collection_name_to_key,
        input_hashes,
        input_paths,
    ) = _validate_frozen_inputs(closure_dir=closure, package_dir=package)
    _validate_kalyanaram_summary(canonical_summary)

    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=str(target.parent))
    ).resolve()
    try:
        build_details = _build_into_staging(
            staging=staging,
            final_bundle=target,
            selected=selected,
            topic_memberships=topic_memberships,
            collection_name_to_key=collection_name_to_key,
            input_hashes=input_hashes,
            input_paths=input_paths,
            source_config=config,
            kalyanaram_summary=canonical_summary,
        )
        audit_bundle(
            staging,
            require_pristine_runtime=True,
            logical_bundle_dir=target,
        )
        os.replace(staging, target)
    except Exception:
        if staging.exists() and staging.parent == target.parent:
            shutil.rmtree(staging)
        raise

    audit = audit_bundle(target, require_pristine_runtime=True)
    return {
        "status": "ready",
        "provider_executed": False,
        "bundle_dir": str(target),
        "corpus_count": CORPUS_SIZE,
        "topic_counts": {
            topic_id: count for topic_id, _name, count in TOPIC_SPECS
        },
        "topic_union_count": CORPUS_SIZE,
        "runtime_output_root": str(target / RUNTIME_OUTPUT_NAME),
        "runtime_output_empty": True,
        "selected_manifest_sha256": file_sha256(
            target / SELECTED_MANIFEST_NAME
        ),
        "parent_spec_sha256": file_sha256(target / PARENT_SPEC_NAME),
        "corpus_hash": build_details["corpus_hash"],
        "materialization": build_details["materialization"],
        "audit": audit,
    }


def audit_bundle(
    bundle_dir: str | Path,
    *,
    require_pristine_runtime: bool = True,
    logical_bundle_dir: str | Path | None = None,
) -> dict[str, Any]:
    bundle = Path(bundle_dir).expanduser().resolve()
    logical_bundle = (
        Path(logical_bundle_dir).expanduser().resolve()
        if logical_bundle_dir is not None
        else bundle
    )
    manifest_path = bundle / SELECTED_MANIFEST_NAME
    bundle_manifest_path = bundle / BUNDLE_MANIFEST_NAME
    report_path = bundle / ZOTERO_REPORT_NAME
    library = bundle / SELECTED_LIBRARY_NAME
    derived_config = bundle / DERIVED_CONFIG_NAME
    parent_spec = bundle / PARENT_SPEC_NAME
    for required in (
        manifest_path,
        bundle_manifest_path,
        report_path,
        derived_config,
        parent_spec,
    ):
        if not required.is_file():
            raise Stage1RebuildError(
                f"bundle is missing required artifact: {required.name}"
            )
    if not library.is_dir():
        raise Stage1RebuildError("bundle selected library is missing")

    manifest = _read_json(manifest_path)
    bundle_manifest = _read_json(bundle_manifest_path)
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != SELECTED_SCHEMA
        or int(manifest.get("corpus_count") or 0) != CORPUS_SIZE
    ):
        raise Stage1RebuildError("selected source manifest schema/count is invalid")
    if (
        not isinstance(bundle_manifest, Mapping)
        or bundle_manifest.get("schema_version") != BUNDLE_SCHEMA
        or bundle_manifest.get("provider_executed") is not False
    ):
        raise Stage1RebuildError("bundle manifest schema/execution state is invalid")

    selected = manifest.get("selected_sources")
    if not isinstance(selected, list) or len(selected) != CORPUS_SIZE:
        raise Stage1RebuildError("selected source manifest does not contain 84 records")
    report_meta = manifest.get("zotero_report")
    if (
        not isinstance(report_meta, Mapping)
        or report_meta.get("path") != ZOTERO_REPORT_NAME
        or report_meta.get("sha256") != file_sha256(report_path)
    ):
        raise Stage1RebuildError("Zotero report hash does not match its manifest")

    expected_filenames: set[str] = set()
    expected_by_parent: dict[str, Mapping[str, Any]] = {}
    for raw_row in selected:
        if not isinstance(raw_row, Mapping):
            raise Stage1RebuildError("selected source record is not an object")
        parent_key = str(raw_row.get("paper_id") or "")
        canonical_key = str(raw_row.get("canonical_paper_key") or "")
        filename = _validate_package_filename(str(raw_row.get("pdf_filename") or ""))
        if (
            not parent_key
            or not canonical_key
            or parent_key in expected_by_parent
            or filename.casefold() in expected_filenames
        ):
            raise Stage1RebuildError("selected source identities are duplicated")
        expected_by_parent[parent_key] = raw_row
        expected_filenames.add(filename.casefold())
        selected_path = _relative_under(
            bundle,
            str(raw_row.get("selected_pdf_path") or ""),
            label="selected PDF path",
        )
        if selected_path.parent != library.resolve() or not selected_path.is_file():
            raise Stage1RebuildError("selected PDF path is outside the selected library")
        if (
            selected_path.stat().st_size != int(raw_row.get("pdf_size_bytes") or -1)
            or file_sha256(selected_path) != raw_row.get("pdf_sha256")
        ):
            raise Stage1RebuildError("selected PDF failed manifest readback")
        source_path = Path(str(raw_row.get("package_pdf_path") or "")).resolve()
        if (
            not source_path.is_file()
            or source_path.stat().st_size != int(raw_row.get("pdf_size_bytes") or -1)
            or file_sha256(source_path) != raw_row.get("pdf_sha256")
        ):
            raise Stage1RebuildError("source package PDF failed provenance readback")

    actual_filenames = {
        path.name.casefold()
        for path in library.iterdir()
        if path.is_file() and path.suffix.casefold() == ".pdf"
    }
    if actual_filenames != expected_filenames:
        raise Stage1RebuildError("selected library contains missing or extra PDFs")
    _verify_report_round_trip(report_path, library, selected)

    selected_manifest_hash = file_sha256(manifest_path)
    union: set[str] = set()
    topic_counts: dict[str, int] = {}
    for topic_id, topic_name, expected_count in TOPIC_SPECS:
        topic_path = bundle / TOPIC_DIRECTORY_NAME / f"{topic_id}_selection.json"
        if not topic_path.is_file():
            raise Stage1RebuildError(f"missing topic selection: {topic_id}")
        payload = _read_json(topic_path)
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != TOPIC_SCHEMA
            or payload.get("topic_id") != topic_id
            or payload.get("collection_name") != topic_name
            or int(payload.get("expected_count") or 0) != expected_count
            or payload.get("source_manifest_sha256") != selected_manifest_hash
        ):
            raise Stage1RebuildError(f"topic selection contract failed: {topic_id}")
        selection_hash = payload.get("selection_hash")
        hash_payload = dict(payload)
        hash_payload.pop("selection_hash", None)
        if selection_hash != _canonical_hash(hash_payload):
            raise Stage1RebuildError(f"topic selection hash failed: {topic_id}")
        parent_keys = [str(item) for item in payload.get("ordered_zotero_parent_keys") or []]
        paper_keys = [str(item) for item in payload.get("ordered_paper_keys") or []]
        if (
            len(parent_keys) != expected_count
            or len(paper_keys) != expected_count
            or len(set(parent_keys)) != expected_count
            or len(set(paper_keys)) != expected_count
        ):
            raise Stage1RebuildError(f"topic selection count/uniqueness failed: {topic_id}")
        expected_paper_keys = [
            str(expected_by_parent[parent_key]["canonical_paper_key"])
            for parent_key in parent_keys
            if parent_key in expected_by_parent
        ]
        if expected_paper_keys != paper_keys:
            raise Stage1RebuildError(f"topic selection identity mapping failed: {topic_id}")
        topic_counts[topic_id] = len(parent_keys)
        union.update(parent_keys)
    if union != set(expected_by_parent):
        raise Stage1RebuildError("topic selection union is not exactly the 84-paper corpus")

    runtime_output_root = bundle / RUNTIME_OUTPUT_NAME
    if require_pristine_runtime:
        _assert_pristine_runtime_output(runtime_output_root)
    parser = configparser.RawConfigParser(interpolation=None)
    try:
        parser.read(derived_config, encoding="utf-8")
        configured_output = Path(
            parser.get("Paths", "output_path", raw=True)
        ).resolve()
        performance_workers = parser.getint("Performance", "max_workers")
        validation_workers = parser.getint("Validation", "max_workers")
    except (configparser.Error, OSError, ValueError) as exc:
        raise Stage1RebuildError("derived config failed readback") from exc
    if (
        configured_output != (logical_bundle / RUNTIME_OUTPUT_NAME).resolve()
        or performance_workers != 1
        or validation_workers != 1
    ):
        raise Stage1RebuildError("derived config runtime controls are invalid")

    spec_payload = _read_json(parent_spec)
    if not isinstance(spec_payload, Mapping):
        raise Stage1RebuildError("parent runtime spec is not an object")
    spec = RuntimeJobSpec.from_dict(spec_payload)
    spec.validate()
    if (
        spec.action != "analyze"
        or spec.reuse_stage1 is not True
        or len(spec.reuse_summary_files) != 1
        or spec.source.zotero_report
        != str(logical_bundle / ZOTERO_REPORT_NAME)
        or spec.source.library_path
        != str(logical_bundle / SELECTED_LIBRARY_NAME)
        or spec.config != str(logical_bundle / DERIVED_CONFIG_NAME)
    ):
        raise Stage1RebuildError("parent runtime spec violates the frozen run contract")
    _validate_kalyanaram_summary(Path(spec.reuse_summary_files[0]))

    artifact_entries = bundle_manifest.get("artifacts")
    if not isinstance(artifact_entries, list):
        raise Stage1RebuildError("bundle manifest artifact list is invalid")
    for artifact in artifact_entries:
        if not isinstance(artifact, Mapping):
            raise Stage1RebuildError("bundle manifest artifact entry is invalid")
        artifact_path = _relative_under(
            bundle,
            str(artifact.get("path") or ""),
            label="bundle artifact path",
        )
        if (
            not artifact_path.is_file()
            or file_sha256(artifact_path) != artifact.get("sha256")
        ):
            raise Stage1RebuildError("bundle artifact hash readback failed")

    return {
        "status": "clean",
        "provider_executed": False,
        "bundle_dir": str(bundle),
        "corpus_count": CORPUS_SIZE,
        "selected_pdf_count": len(actual_filenames),
        "topic_counts": topic_counts,
        "topic_union_count": len(union),
        "runtime_output_empty": not any(runtime_output_root.iterdir()),
        "reuse_summary_count": len(spec.reuse_summary_files),
        "reuse_summary_sha256": KALYANARAM_SUMMARY_SHA256,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare/audit the frozen 84-paper PPH Stage 1 rebuild corpus."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--closure-dir", type=Path, required=True)
    build.add_argument("--package-dir", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--source-config", type=Path, default=DEFAULT_SOURCE_CONFIG)
    build.add_argument(
        "--canonical-kalyanaram-summary",
        type=Path,
        default=DEFAULT_KALYANARAM_SUMMARY,
    )

    spec = subparsers.add_parser("build-spec")
    spec.add_argument("--bundle-dir", type=Path, required=True)
    spec.add_argument("--project-name", default="pph_master_stage1_rebuild_56570")
    spec.add_argument("--output", type=Path)
    spec.add_argument(
        "--canonical-kalyanaram-summary",
        type=Path,
        default=DEFAULT_KALYANARAM_SUMMARY,
    )

    audit = subparsers.add_parser("audit")
    audit.add_argument("--bundle-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_rebuild_bundle(
                closure_dir=args.closure_dir,
                package_dir=args.package_dir,
                output_dir=args.output_dir,
                source_config=args.source_config,
                kalyanaram_summary=args.canonical_kalyanaram_summary,
            )
        elif args.command == "build-spec":
            result = build_parent_spec(
                args.bundle_dir,
                project_name=args.project_name,
                output_path=args.output,
                kalyanaram_summary=args.canonical_kalyanaram_summary,
            )
        else:
            result = audit_bundle(args.bundle_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (Stage1RebuildError, ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

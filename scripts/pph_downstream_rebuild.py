"""Derive deterministic downstream PPH review inputs from the frozen 84-paper corpus.

This module deliberately performs no provider or model work.  It only verifies the
accepted Stage 1 corpus and copies complete, unchanged summary objects into the
frozen topic subsets and combined review inputs.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_EXPECTED_COUNT = 84

SUBSET_SPECS: dict[str, str] = {
    "S01": "01_dynamic_pricing_summaries.json",
    "S02": "02_platform_concession_summaries.json",
    "S03": "03_concession_unfairness_summaries.json",
    "S04": "04_unfairness_continuance_summaries.json",
    "S05": "05_subjective_knowledge_summaries.json",
    "S90": "90_experience_awareness_summaries.json",
    "S91": "91_boundary_ethics_summaries.json",
}

EXPECTED_SELECTION_COUNTS: dict[str, int] = {
    "S01": 19,
    "S02": 21,
    "S03": 25,
    "S04": 19,
    "S05": 15,
    "S90": 6,
    "S91": 7,
}

REVIEW_INPUT_SPECS: dict[str, tuple[str, ...]] = {
    "S01": ("S01", "S91"),
    "S02": ("S02", "S90", "S91"),
    "S03": ("S03", "S01", "S02", "S91"),
    "S04": ("S04", "S03"),
    "S05": ("S05", "S02", "S03"),
}

PROHIBITED_SOURCE_MARKERS = ("schuhmacher", "ssrn")


class DerivationError(RuntimeError):
    """Raised when a frozen input fails an integrity check."""


@dataclass(frozen=True)
class DerivationPaths:
    canonical_summaries: Path
    canonical_manifest: Path
    selection_dir: Path
    output_root: Path

    @classmethod
    def from_repo_root(cls, repo_root: str | Path) -> "DerivationPaths":
        root = Path(repo_root).expanduser().resolve()
        parent_work = root / "tmp" / "pph_stage1_parent_work_56570_20260729_crosswalk"
        rebuild_work = root / "tmp" / "pph_stage1_rebuild_56570_20260729_v1"
        return cls(
            canonical_summaries=parent_work / "stage1_canonical_84_summaries.json",
            canonical_manifest=parent_work / "stage1_canonical_84_manifest.json",
            selection_dir=rebuild_work / "topic_selections",
            output_root=root
            / "output"
            / "pph_review_work"
            / "corrected_stage1_84",
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, *, label: str) -> Any:
    if not path.is_file():
        raise DerivationError(f"{label} does not exist or is not a file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DerivationError(f"{label} is not valid UTF-8 JSON: {path}: {exc}") from exc


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.replace(path)


def _paper_identity(record: Mapping[str, Any], *, label: str) -> tuple[str, str]:
    if str(record.get("status") or "").strip().casefold() != "success":
        raise DerivationError(f"{label} is not a successful Stage 1 summary")
    paper_info = record.get("paper_info")
    if not isinstance(paper_info, Mapping):
        raise DerivationError(f"{label} has no paper_info object")
    canonical_key = str(paper_info.get("canonical_paper_key") or "").strip()
    zotero_key = str(paper_info.get("zotero_parent_key") or "").strip()
    if not canonical_key:
        raise DerivationError(f"{label} has no canonical_paper_key")
    if not zotero_key:
        raise DerivationError(f"{label} has no zotero_parent_key")
    return canonical_key, zotero_key


def _assert_no_prohibited_sources(records: Sequence[Mapping[str, Any]]) -> None:
    """Reject excluded formal sources before any downstream artifact is written."""
    for index, record in enumerate(records, start=1):
        paper_info = record.get("paper_info")
        if not isinstance(paper_info, Mapping):
            continue
        searchable = " ".join(
            str(paper_info.get(field) or "")
            for field in (
                "title",
                "canonical_paper_key",
                "source_paper_id",
                "doi",
                "url",
            )
        ).casefold()
        marker = next(
            (candidate for candidate in PROHIBITED_SOURCE_MARKERS if candidate in searchable),
            None,
        )
        if marker:
            raise DerivationError(
                f"canonical summaries includes prohibited formal source marker "
                f"{marker!r} at entry {index}"
            )


def load_canonical_corpus(
    paths: DerivationPaths, *, expected_count: int = CANONICAL_EXPECTED_COUNT
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load and validate the accepted canonical Stage 1 corpus and manifest."""
    manifest = _read_json(paths.canonical_manifest, label="canonical manifest")
    summaries = _read_json(paths.canonical_summaries, label="canonical summaries")
    if not isinstance(manifest, Mapping):
        raise DerivationError("canonical manifest must be a JSON object")
    if not isinstance(summaries, list):
        raise DerivationError("canonical summaries must be a JSON array")

    expected_hash = str(manifest.get("canonical_summaries_sha256") or "").strip()
    actual_hash = _file_sha256(paths.canonical_summaries)
    if not expected_hash or expected_hash != actual_hash:
        raise DerivationError(
            "canonical summary SHA-256 does not match the canonical manifest"
        )

    for field in (
        "expected_corpus_count",
        "canonical_summary_count",
        "unique_canonical_paper_key_count",
        "unique_zotero_parent_key_count",
    ):
        if manifest.get(field) != expected_count:
            raise DerivationError(
                f"canonical manifest {field} must equal {expected_count}, got "
                f"{manifest.get(field)!r}"
            )
    if len(summaries) != expected_count:
        raise DerivationError(
            f"canonical summaries must contain exactly {expected_count} records, "
            f"got {len(summaries)}"
        )

    validated: list[dict[str, Any]] = []
    paper_indexes: dict[str, int] = {}
    zotero_indexes: dict[str, int] = {}
    for index, raw_record in enumerate(summaries, start=1):
        if not isinstance(raw_record, Mapping):
            raise DerivationError(f"canonical summaries entry {index} is not an object")
        record = dict(raw_record)
        paper_key, zotero_key = _paper_identity(
            record, label=f"canonical summaries entry {index}"
        )
        paper_identity = paper_key.casefold()
        zotero_identity = zotero_key.casefold()
        if paper_identity in paper_indexes:
            raise DerivationError(
                "canonical summaries has duplicate canonical_paper_key at entries "
                f"{paper_indexes[paper_identity]} and {index}: {paper_key}"
            )
        if zotero_identity in zotero_indexes:
            raise DerivationError(
                "canonical summaries has duplicate zotero_parent_key at entries "
                f"{zotero_indexes[zotero_identity]} and {index}: {zotero_key}"
            )
        paper_indexes[paper_identity] = index
        zotero_indexes[zotero_identity] = index
        validated.append(record)

    _assert_no_prohibited_sources(validated)

    manifest_papers = manifest.get("papers")
    if not isinstance(manifest_papers, list) or len(manifest_papers) != expected_count:
        raise DerivationError("canonical manifest papers must cover the full canonical corpus")
    for index, (record, manifest_paper) in enumerate(
        zip(validated, manifest_papers), start=1
    ):
        if not isinstance(manifest_paper, Mapping):
            raise DerivationError(f"canonical manifest papers entry {index} is not an object")
        paper_key, zotero_key = _paper_identity(
            record, label=f"canonical summaries entry {index}"
        )
        if str(manifest_paper.get("canonical_paper_key") or "").strip() != paper_key:
            raise DerivationError(f"canonical manifest paper identity mismatch at entry {index}")
        if str(manifest_paper.get("zotero_parent_key") or "").strip() != zotero_key:
            raise DerivationError(f"canonical manifest Zotero identity mismatch at entry {index}")

    return validated, dict(manifest)


def _load_selection(
    path: Path,
    *,
    expected_topic: str,
    canonical_by_key: Mapping[str, Mapping[str, Any]],
    expected_source_manifest_sha256: str,
    expected_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = _read_json(path, label=f"selection {expected_topic}")
    if not isinstance(payload, Mapping):
        raise DerivationError(f"selection {expected_topic} must be a JSON object")
    selection = dict(payload)
    selection_hash = str(selection.pop("selection_hash", "") or "")
    if selection_hash != _canonical_hash(selection):
        raise DerivationError(f"selection {expected_topic} has an invalid selection_hash")
    if selection.get("artifact_type") != "stage1_exact_set_selection":
        raise DerivationError(f"selection {expected_topic} has an invalid artifact_type")
    if selection.get("schema_version") != "pph-stage1-exact-set-v1":
        raise DerivationError(f"selection {expected_topic} has an invalid schema_version")
    if selection.get("topic_id") != expected_topic:
        raise DerivationError(
            f"selection {expected_topic} topic_id mismatch: {selection.get('topic_id')!r}"
        )
    if str(selection.get("source_manifest_sha256") or "") != expected_source_manifest_sha256:
        raise DerivationError(
            f"selection {expected_topic} does not reference the accepted source manifest"
        )

    paper_keys = selection.get("ordered_paper_keys")
    zotero_keys = selection.get("ordered_zotero_parent_keys")
    declared_count = selection.get("expected_count")
    if not isinstance(declared_count, int) or declared_count < 1:
        raise DerivationError(f"selection {expected_topic} expected_count must be a positive integer")
    if declared_count != expected_count:
        raise DerivationError(
            f"selection {expected_topic} expected_count must equal {expected_count}, "
            f"got {declared_count}"
        )
    if not isinstance(paper_keys, list) or not isinstance(zotero_keys, list):
        raise DerivationError(
            f"selection {expected_topic} must contain paired ordered paper and Zotero keys"
        )
    if len(paper_keys) != expected_count or len(zotero_keys) != expected_count:
        raise DerivationError(
            f"selection {expected_topic} expected_count {expected_count} does not match "
            "its paired ordered key lists"
        )

    ordered_records: list[dict[str, Any]] = []
    seen_paper_keys: set[str] = set()
    for index, (raw_paper_key, raw_zotero_key) in enumerate(
        zip(paper_keys, zotero_keys), start=1
    ):
        paper_key = str(raw_paper_key).strip()
        zotero_key = str(raw_zotero_key).strip()
        identity = paper_key.casefold()
        if not paper_key or not zotero_key:
            raise DerivationError(f"selection {expected_topic} has an empty identity at entry {index}")
        if identity in seen_paper_keys:
            raise DerivationError(
                f"selection {expected_topic} has duplicate canonical_paper_key: {paper_key}"
            )
        canonical = canonical_by_key.get(identity)
        if canonical is None:
            raise DerivationError(
                f"selection {expected_topic} references missing canonical_paper_key: {paper_key}"
            )
        _, canonical_zotero_key = _paper_identity(
            canonical, label=f"canonical record for selection {expected_topic}"
        )
        if canonical_zotero_key.casefold() != zotero_key.casefold():
            raise DerivationError(
                f"selection {expected_topic} identity mismatch for {paper_key}: "
                f"expected Zotero {canonical_zotero_key}, got {zotero_key}"
            )
        seen_paper_keys.add(identity)
        ordered_records.append(dict(canonical))

    selection["selection_hash"] = selection_hash
    return selection, ordered_records


def _ordered_unique_union(groups: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for record in group:
            paper_key, _ = _paper_identity(record, label="derived subset entry")
            identity = paper_key.casefold()
            if identity not in seen:
                seen.add(identity)
                ordered.append(dict(record))
    return ordered


def derive_subsets(
    paths: DerivationPaths,
    *,
    expected_canonical_count: int = CANONICAL_EXPECTED_COUNT,
    expected_selection_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Validate frozen inputs and write deterministic subsets, inputs, and audit files."""
    canonical_records, canonical_manifest = load_canonical_corpus(
        paths, expected_count=expected_canonical_count
    )
    canonical_by_key = {
        _paper_identity(record, label="canonical record")[0].casefold(): record
        for record in canonical_records
    }
    source_manifest_sha256 = str(canonical_manifest.get("selected_manifest_sha256") or "")
    if not source_manifest_sha256:
        raise DerivationError("canonical manifest has no selected_manifest_sha256")

    selection_counts = dict(expected_selection_counts or EXPECTED_SELECTION_COUNTS)
    if set(selection_counts) != set(SUBSET_SPECS):
        raise DerivationError("expected selection counts must cover exactly S01-S05, S90, and S91")

    selections: dict[str, dict[str, Any]] = {}
    subsets: dict[str, list[dict[str, Any]]] = {}
    for topic_id in SUBSET_SPECS:
        selection_path = paths.selection_dir / f"{topic_id}_selection.json"
        selection, records = _load_selection(
            selection_path,
            expected_topic=topic_id,
            canonical_by_key=canonical_by_key,
            expected_source_manifest_sha256=source_manifest_sha256,
            expected_count=selection_counts[topic_id],
        )
        selections[topic_id] = selection
        subsets[topic_id] = records

    subset_root = paths.output_root / "subset_summaries"
    review_input_root = paths.output_root / "review_inputs"
    subset_rows: list[dict[str, Any]] = []
    traceability_rows: list[dict[str, Any]] = []
    for topic_id, filename in SUBSET_SPECS.items():
        records = subsets[topic_id]
        subset_path = subset_root / filename
        _atomic_write_json(subset_path, records)
        summary_hash = _file_sha256(subset_path)
        manifest_path = subset_root / f"{Path(filename).stem}_manifest.json"
        subset_manifest = {
            "artifact_type": "pph_downstream_subset_manifest",
            "schema_version": "pph-downstream-subset-v1",
            "topic_id": topic_id,
            "expected_count": selections[topic_id]["expected_count"],
            "summary_count": len(records),
            "summary_path": filename,
            "summary_sha256": summary_hash,
            "selection_hash": selections[topic_id]["selection_hash"],
            "selection_path": str(paths.selection_dir / f"{topic_id}_selection.json"),
            "canonical_summaries_path": str(paths.canonical_summaries),
            "canonical_summaries_sha256": _file_sha256(paths.canonical_summaries),
            "canonical_paper_keys": [
                _paper_identity(record, label="derived subset entry")[0]
                for record in records
            ],
            "zotero_parent_keys": [
                _paper_identity(record, label="derived subset entry")[1]
                for record in records
            ],
            "provider_call_count": 0,
            "model_call_count": 0,
        }
        _atomic_write_json(manifest_path, subset_manifest)
        subset_rows.append(
            {
                "topic_id": topic_id,
                "summary_filename": filename,
                "summary_count": len(records),
                "summary_sha256": summary_hash,
                "manifest_filename": manifest_path.name,
                "selection_hash": selections[topic_id]["selection_hash"],
                "canonical_summaries_sha256": _file_sha256(paths.canonical_summaries),
            }
        )
        for record in records:
            paper_info = record["paper_info"]
            paper_key, zotero_key = _paper_identity(
                record, label="derived subset entry"
            )
            traceability_rows.append(
                {
                    "paper_id": str(paper_info.get("paper_id") or zotero_key),
                    "title": str(paper_info.get("title") or ""),
                    "master_summary_key": paper_key,
                    "collection_id": str(selections[topic_id].get("collection_key") or ""),
                    "collection_name": str(selections[topic_id].get("collection_name") or ""),
                    "eligibility_status": str(
                        paper_info.get("eligibility_status") or "eligible"
                    ),
                    "included_in_subset": "true",
                    "topic_id": topic_id,
                    "zotero_parent_key": zotero_key,
                    "summary_filename": filename,
                    "summary_sha256": summary_hash,
                    "selection_hash": selections[topic_id]["selection_hash"],
                    "canonical_summaries_sha256": _file_sha256(
                        paths.canonical_summaries
                    ),
                }
            )

    csv_path = subset_root / "subset_summary_manifest.csv"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=subset_root, delete=False, newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(traceability_rows[0]))
        writer.writeheader()
        writer.writerows(traceability_rows)
        temporary_csv = Path(handle.name)
    temporary_csv.replace(csv_path)

    combined_inputs: dict[str, dict[str, Any]] = {}
    for topic_id, component_topics in REVIEW_INPUT_SPECS.items():
        records = _ordered_unique_union([subsets[source] for source in component_topics])
        output_path = review_input_root / f"{topic_id}_review_input_summaries.json"
        _atomic_write_json(output_path, records)
        combined_inputs[topic_id] = {
            "component_topics": list(component_topics),
            "summary_count": len(records),
            "summary_path": str(output_path),
            "summary_sha256": _file_sha256(output_path),
            "canonical_paper_keys": [
                _paper_identity(record, label="derived review input entry")[0]
                for record in records
            ],
        }

    audit = {
        "artifact_type": "pph_downstream_rebuild_audit",
        "schema_version": "pph-downstream-rebuild-v1",
        "created_at": _utc_now(),
        "status": "clean",
        "provider_call_count": 0,
        "model_call_count": 0,
        "zero_model_calls_asserted": True,
        "traceability_verdict": "pass",
        "canonical": {
            "summary_count": len(canonical_records),
            "summary_path": str(paths.canonical_summaries),
            "summary_sha256": _file_sha256(paths.canonical_summaries),
            "manifest_path": str(paths.canonical_manifest),
            "manifest_sha256": _file_sha256(paths.canonical_manifest),
        },
        "subsets": subset_rows,
        "review_inputs": combined_inputs,
        "traceability": {
            topic_id: {
                "selection_path": str(paths.selection_dir / f"{topic_id}_selection.json"),
                "selection_hash": selection["selection_hash"],
                "source_manifest_sha256": selection["source_manifest_sha256"],
            }
            for topic_id, selection in selections.items()
        },
    }
    if audit["provider_call_count"] != 0 or audit["model_call_count"] != 0:
        raise AssertionError("downstream derivation must not make provider or model calls")

    audit_path = paths.output_root / "downstream_rebuild_audit.json"
    _atomic_write_json(audit_path, audit)
    audit_markdown = "\n".join(
        [
            "# PPH Downstream Rebuild Audit",
            "",
            "- Status: clean",
            f"- Canonical summaries: {len(canonical_records)}",
            "- Provider calls: 0",
            "- Model calls: 0",
            "",
            "## Subsets",
            "",
            "| Topic | Count | SHA-256 |",
            "| --- | ---: | --- |",
            *[
                f"| {row['topic_id']} | {row['summary_count']} | {row['summary_sha256']} |"
                for row in subset_rows
            ],
            "",
            "## Combined Review Inputs",
            "",
            "| Review | Sources | Count | SHA-256 |",
            "| --- | --- | ---: | --- |",
            *[
                f"| {topic_id} | {' + '.join(item['component_topics'])} | "
                f"{item['summary_count']} | {item['summary_sha256']} |"
                for topic_id, item in combined_inputs.items()
            ],
            "",
        ]
    )
    _atomic_write_text(paths.output_root / "downstream_rebuild_audit.md", audit_markdown)
    return audit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    derive = subparsers.add_parser(
        "derive-subsets", help="derive provider-free downstream PPH review inputs"
    )
    derive.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    derive.add_argument("--canonical-summaries", type=Path)
    derive.add_argument("--canonical-manifest", type=Path)
    derive.add_argument("--selection-dir", type=Path)
    derive.add_argument("--output-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = DerivationPaths.from_repo_root(args.repo_root)
    paths = DerivationPaths(
        canonical_summaries=(args.canonical_summaries or paths.canonical_summaries).resolve(),
        canonical_manifest=(args.canonical_manifest or paths.canonical_manifest).resolve(),
        selection_dir=(args.selection_dir or paths.selection_dir).resolve(),
        output_root=(args.output_root or paths.output_root).resolve(),
    )
    try:
        audit = derive_subsets(paths)
    except DerivationError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": audit["status"],
                "output_root": str(paths.output_root),
                "canonical_count": audit["canonical"]["summary_count"],
                "model_call_count": audit["model_call_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

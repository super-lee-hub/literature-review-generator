"""Literature map builder for Outline Intelligence v2.

Derives paper nodes from summaries and paper artifacts.
Conservative: unknowns become diagnostics, not fabricated claims.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from outline.v2_models import (
    LiteratureMap,
    PaperNode,
    compute_content_hash,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_summary(summary: Dict[str, Any]) -> str:
    content = json.dumps(summary, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _safe_str(value: Any, default: str = "") -> str:
    return str(value).strip() if value else default


def _extract_paper_node(summary: Dict[str, Any], index: int) -> PaperNode:
    """Extract a paper node from a summary dict.

    Summary may have paper_info, title, authors, year, themes, methods, etc.
    Unknown fields become empty/diagnostics — nothing is fabricated.
    """
    paper_info = summary.get("paper_info", summary)
    if not isinstance(paper_info, dict):
        paper_info = {}

    title = _safe_str(paper_info.get("title", summary.get("title", "")))
    authors = paper_info.get("authors", summary.get("authors", []))
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(";") if a.strip()]
    if not isinstance(authors, list):
        authors = []

    year = paper_info.get("year", summary.get("year"))
    if year is not None:
        try:
            year = int(year)
        except (ValueError, TypeError):
            year = None

    themes = summary.get("themes", paper_info.get("themes", []))
    if isinstance(themes, str):
        themes = [t.strip() for t in themes.split(";") if t.strip()]
    if not isinstance(themes, list):
        themes = []

    methods = summary.get("methods", paper_info.get("methods", []))
    if isinstance(methods, str):
        methods = [m.strip() for m in methods.split(";") if m.strip()]
    if not isinstance(methods, list):
        methods = []

    theories = summary.get("theories", paper_info.get("theories", []))
    if isinstance(theories, str):
        theories = [t.strip() for t in theories.split(";") if t.strip()]
    if not isinstance(theories, list):
        theories = []

    limitations = summary.get("limitations", paper_info.get("limitations", []))
    if isinstance(limitations, str):
        limitations = [l.strip() for l in limitations.split(";") if l.strip()]
    if not isinstance(limitations, list):
        limitations = []

    abstract = _safe_str(summary.get("abstract", paper_info.get("abstract", "")))
    source_hash = _hash_summary(summary)
    paper_key = f"paper_{source_hash[:8]}_{index:03d}"

    # Determine classification (conservative: default "support")
    classification = _safe_str(paper_info.get("classification", "support"), "support")
    must_use = str(paper_info.get("must_use", "false")).lower() in {"true", "1", "yes"}
    if classification == "core":
        must_use = True

    # Diagnostics for missing data
    diagnostics: List[str] = []
    if not title:
        diagnostics.append(f"missing_title for summary index {index}")
    if not authors:
        diagnostics.append(f"missing_authors for {title or f'summary index {index}'}")
    if year is None:
        diagnostics.append(f"missing_year for {title or f'summary index {index}'}")
    if not themes:
        diagnostics.append(f"missing_themes for {title or f'summary index {index}'}")
    if not abstract:
        diagnostics.append(f"missing_abstract for {title or f'summary index {index}'}")

    return PaperNode(
        paper_key=paper_key,
        source_summary_hash=source_hash,
        title=title,
        authors=authors,
        year=year,
        abstract_snippet=abstract[:500] if abstract else "",
        themes=themes,
        methods=methods,
        theories=theories,
        limitations=limitations,
        classification=classification,
        must_use=must_use,
        diagnostics=diagnostics,
    )


def build_literature_map(
    summaries: Sequence[Dict[str, Any]],
    job_id: str,
    paper_artifacts: Sequence[Dict[str, Any]] | None = None,
) -> LiteratureMap:
    """Build a literature_map from summaries and optional paper artifacts.

    Every source summary maps to a paper node or a blocking diagnostic.
    """
    paper_nodes: List[PaperNode] = []
    source_hashes: List[str] = []
    blocking_diagnostics: List[Dict[str, str]] = []

    for i, summary in enumerate(summaries):
        if not isinstance(summary, dict):
            blocking_diagnostics.append({
                "type": "invalid_summary_format",
                "index": str(i),
                "message": f"Summary at index {i} is not a dict",
            })
            continue

        source_hash = _hash_summary(summary)
        source_hashes.append(source_hash)

        try:
            node = _extract_paper_node(summary, i)
            paper_nodes.append(node)
        except Exception as exc:
            blocking_diagnostics.append({
                "type": "extraction_failure",
                "index": str(i),
                "message": str(exc),
            })

    # Also process paper_artifacts if provided
    if paper_artifacts:
        for pa in paper_artifacts:
            if not isinstance(pa, dict):
                continue
            try:
                node = _extract_paper_node(pa, len(paper_nodes))
                paper_nodes.append(node)
                source_hashes.append(_hash_summary(pa))
            except Exception:
                pass

    # Build classification map
    paper_classification: Dict[str, List[str]] = {
        "core": [],
        "background_only": [],
        "peripheral": [],
        "support": [],
    }
    for node in paper_nodes:
        cls = node.classification if node.classification in paper_classification else "support"
        paper_classification[cls].append(node.paper_key)

    # Simple stream detection based on themes
    research_streams: List[Dict[str, Any]] = []
    theme_set: Dict[str, List[str]] = {}
    for node in paper_nodes:
        for theme in node.themes:
            theme_set.setdefault(theme, []).append(node.paper_key)

    for theme, papers in theme_set.items():
        research_streams.append({"stream_name": theme, "paper_keys": papers})

    return LiteratureMap(
        created_from_job_id=job_id,
        created_at=_utc_now_iso(),
        source_summary_hashes=source_hashes,
        paper_nodes=paper_nodes,
        research_streams=research_streams,
        theoretical_dimensions=[],
        method_clusters=[],
        empirical_contexts=[],
        key_tensions=[],
        candidate_gaps=[],
        paper_classification=paper_classification,
        blocking_diagnostics=blocking_diagnostics,
    )

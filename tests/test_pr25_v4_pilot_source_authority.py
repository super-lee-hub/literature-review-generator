"""Downstream topic pilots retain typed Stage 1 reuse authority checks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.orchestrator import InternalStageExecutorRegistry


def _session(path: Path, *, cached: bool = False) -> SimpleNamespace:
    row = {"paper_info": {"title": "Example"}, "ai_summary": {}}
    return SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=(str(path),),
            reuse_summary_files=(),
        ),
        stage_host=SimpleNamespace(summaries=[row] if cached else []),
    )


@pytest.mark.parametrize("cached", [False, True])
def test_pilot_rejects_unverified_summary_source(tmp_path: Path, cached: bool) -> None:
    source = tmp_path / "summary.json"
    source.write_text(
        json.dumps([{"paper_info": {"title": "Example"}, "ai_summary": {}}]),
        encoding="utf-8",
    )
    bridge = SimpleNamespace(
        job_spec=SimpleNamespace(
            metadata={"outline_pilot": {"schema_version": "outline-topic-pilot/v1"}},
            summary_sources=(str(source),),
        )
    )
    executor = InternalStageExecutorRegistry(bridge)

    with pytest.raises(RuntimeError, match="typed Stage 1 manifest authority"):
        executor._load_summary_payloads(_session(source, cached=cached), {})


def test_normal_downstream_summary_source_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "summary.json"
    source.write_text(
        json.dumps([{"paper_info": {"title": "Example"}, "ai_summary": {}}]),
        encoding="utf-8",
    )
    bridge = SimpleNamespace(
        job_spec=SimpleNamespace(metadata={}, summary_sources=(str(source),))
    )
    executor = InternalStageExecutorRegistry(bridge)

    assert len(executor._load_summary_payloads(_session(source), {})) == 1

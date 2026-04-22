from __future__ import annotations

import json
from pathlib import Path

from tests.test_runtime_bridge_helpers import build_success_summary, make_bridge_session


def test_runtime_review_chain_persists_outline_draft_manifest_and_docx(tmp_path: Path) -> None:
    bridge, session, _pdf_dir, pdf_path = make_bridge_session(tmp_path, action="generate_review")

    bridge.persist_stage1_results(
        session,
        [build_success_summary(pdf_path)],
    )

    outline_result = bridge.persist_outline(
        session,
        "# Demo Outline\n\n## 1. Findings\n\n## 2. Discussion",
    )
    outline_path = outline_result.artifacts[0].path

    review_result = bridge.persist_review_chain(
        session,
        outline_file=outline_path,
        review_sections=[
            {
                "section_number": 1,
                "section_title": "Findings",
                "content": "Paper A demonstrates a stable result. [[cite:paper_a|mode=parenthetical]]",
            },
            {
                "section_number": 2,
                "section_title": "Discussion",
                "content": "The discussion revisits the same source. [[cite:paper_a|mode=parenthetical]]",
            },
        ],
    )

    registry_payload = json.loads(Path(session.context.workspace.paths.registry_path).read_text(encoding="utf-8"))
    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}
    review_draft_path = Path(session.generator._review_draft_v2_path())
    citation_manifest_path = Path(session.generator._citation_manifest_path())
    word_path = Path(session.generator._get_review_word_file_path())

    assert review_result.success is True
    assert review_draft_path.exists() is True
    assert citation_manifest_path.exists() is True
    assert word_path.exists() is True
    assert "literature_review_outline" in artifact_types
    assert "review_draft" in artifact_types
    assert "citation_manifest" in artifact_types
    assert "review_docx" in artifact_types

    review_draft = json.loads(review_draft_path.read_text(encoding="utf-8"))
    citation_manifest = json.loads(citation_manifest_path.read_text(encoding="utf-8"))
    assert review_draft["artifact_version"] == "v2"
    assert citation_manifest["artifact_version"] == "v3"

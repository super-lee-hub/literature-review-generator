from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from docx import Document

from docx_writer import (
    append_review_section_blocks_to_word_document,
    rebuild_review_docx_from_structured_artifacts,
    scan_docx_for_unresolved_citation_tokens,
)
from services.artifact_registry import ArtifactRegistry
from services.citation_manifest import build_citation_manifest_from_review_draft
from services.job_workspace import JobWorkspace
from services.review_draft import build_review_draft
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from tests.test_current_review_generation import _stage1_summary
from tests.test_docx_citation_renderer import _manifest


def test_review_service_pipe_table_renders_as_cjk_multipage_native_docx_table(
    tmp_path: Path,
) -> None:
    summary, _source_pdf = _stage1_summary(tmp_path)
    paper_key = str(summary["paper_info"]["canonical_paper_key"])
    long_cjk_paragraph = (
        "中文分页验证文本用于检查真实 Review 服务生成的 Word 连续分页、字体回退和表格布局。"
        * 360
    ) + " [[cite_ref:R001]]"
    markdown_table = "\n".join(
        (
            "| 指标 | 结果 |",
            "| --- | --- |",
            "| 样本量 | 24 名参与者 [[cite_ref:R001]] |",
            "| 排版范围 | 本地中文表格验证 |",
        )
    )

    workspace = JobWorkspace.create(
        str(tmp_path / "review-output"),
        "current",
        job_id="docx-table-job",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id="docx-table-attempt",
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config({
            "Writer_API": {
                "api_key": "writer-test",
                "model": "writer-test",
                "api_base": "https://writer.test/v1",
            },
        }),
        summaries=[summary],
        writer=lambda **_kwargs: {
            "status": "success",
            "content": {
                "blocks": [
                    {"text": long_cjk_paragraph},
                    {"text": markdown_table},
                ],
            },
        },
    )
    result = service.run(
        outline_payload={
            "title": "本地排版验证",
            "sections": [{
                "section_id": "section:results",
                "title": "中文结果",
                "goal": "展示长段落与表格",
            }],
        },
        evidence_packets=[{
            "section_id": "section:results",
            "section_goal": "展示长段落与表格",
            "planned_claims": ["The controlled result is included for renderer QA."],
            "paper_keys": [paper_key],
            "source_summary_hashes": ["docx-table-summary-hash"],
            "retrieval_provenance": {
                "source": "local_docx_table_fixture",
                "paper_keys": [paper_key],
            },
        }],
    )

    # The production Writer service preserves the table as a Markdown block
    # within its current text-only block contract. The DOCX renderer promotes
    # only a complete, unambiguous pipe table to a native Word table.
    assert len(result.sections) == 1
    assert len(result.sections[0]["blocks"]) == 2
    assert result.sections[0]["blocks"][1]["block_kind"] == "paragraph"
    assert result.sections[0]["blocks"][1]["text"] == markdown_table

    output_path = tmp_path / "review-with-native-table.docx"
    review_draft = build_review_draft(
        job_id=workspace.job_id,
        project_name=workspace.project_name,
        draft_id="docx-table-draft",
        outline_artifact_id="outline-v3:final_outline",
        title="本地排版验证",
        outline_source_path="local:docx-table-outline",
        summary_file="local:docx-table-summary",
        review_word_path=str(output_path),
        sections=result.sections,
        references=[],
        generation_mode="review_v3",
        paper_summaries=[summary],
        citation_ref_catalog=result.citation_ref_catalog,
        citation_ref_catalog_path=result.citation_ref_catalog_path,
        citation_ref_catalog_hash=str(
            result.citation_ref_catalog.get("catalog_hash") or ""
        ),
    )
    review_draft_payload = review_draft.to_dict()
    citation_manifest = build_citation_manifest_from_review_draft(
        job_id=workspace.job_id,
        project_name=workspace.project_name,
        manifest_id="docx-table-citation-manifest",
        review_draft_path=str(tmp_path / "review-draft.json"),
        review_word_path=str(output_path),
        review_draft=review_draft_payload,
        paper_summaries=[summary],
        citation_ref_catalog=result.citation_ref_catalog,
        citation_ref_catalog_path=result.citation_ref_catalog_path,
        citation_ref_catalog_hash=str(
            result.citation_ref_catalog.get("catalog_hash") or ""
        ),
    )

    rebuild_review_docx_from_structured_artifacts(
        SimpleNamespace(logger=None),
        review_draft_payload,
        citation_manifest.to_dict(),
        str(output_path),
    )

    document = Document(str(output_path))
    assert document.sections[0].header.paragraphs[0].text == "本地排版验证"
    assert document.sections[0].footer._element.xml.count("PAGE") == 1
    assert len(document.tables) == 1
    table = document.tables[0]
    assert len(table.rows) == 3
    assert len(table.columns) == 2
    table_text = "\n".join(
        cell.text for row in table.rows for cell in row.cells
    )
    assert "指标" in table_text
    assert "样本量" in table_text
    assert "本地中文表格验证" in table_text
    assert "[[cite_ref:" not in table_text
    assert "(" in table_text and ")" in table_text
    assert any("中文分页验证文本" in paragraph.text for paragraph in document.paragraphs)

    scan = scan_docx_for_unresolved_citation_tokens(
        str(output_path),
        citation_manifest.to_dict(),
    )
    assert scan["passed"] is True, scan


def test_legacy_review_draft_uses_default_docx_header_without_duplicate_footer(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "legacy-review.docx"
    review_draft = {
        "draft_identity": {"draft_id": "legacy-draft"},
        "content": {
            "sections": [{
                "section_number": 1,
                "section_title": "Results",
                "blocks": [{"block_kind": "paragraph", "text": "A local result."}],
            }],
            "references": [],
        },
    }

    rebuild_review_docx_from_structured_artifacts(
        SimpleNamespace(logger=None),
        review_draft,
        {"bibliography": []},
        str(output_path),
    )

    document = Document(str(output_path))
    assert document.sections[0].header.paragraphs[0].text == "Literature Review"
    assert document.sections[0].footer._element.xml.count("PAGE") == 1


def test_explicit_table_block_is_native_and_malformed_table_fails_closed(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "explicit-table.docx"
    appended = append_review_section_blocks_to_word_document(
        SimpleNamespace(logger=None),
        1,
        "Results",
        [{
            "block_id": "s1_b1",
            "block_kind": "table",
            "text": "| Measure | Result |\n| --- | --- |\n| Sample | 24 |",
        }],
        str(output_path),
        citation_manifest={},
    )

    assert appended is True
    assert len(Document(str(output_path)).tables) == 1

    malformed_path = tmp_path / "malformed-table.docx"
    malformed_appended = append_review_section_blocks_to_word_document(
        SimpleNamespace(logger=None),
        1,
        "Results",
        [{
            "block_id": "s1_b2",
            "block_kind": "table",
            "text": "| Measure | Result |\nThis is missing a separator row.",
        }],
        str(malformed_path),
        citation_manifest={},
    )

    assert malformed_appended is False
    assert not malformed_path.exists()

    empty_path = tmp_path / "empty-table.docx"
    empty_appended = append_review_section_blocks_to_word_document(
        SimpleNamespace(logger=None),
        1,
        "Results",
        [{"block_id": "s1_b3", "block_kind": "table", "text": "  "}],
        str(empty_path),
        citation_manifest={},
    )
    assert empty_appended is False
    assert not empty_path.exists()


def test_ordinary_prose_with_pipe_remains_a_paragraph(tmp_path: Path) -> None:
    output_path = tmp_path / "ordinary-pipe-prose.docx"
    prose = "The expression A | B is a comparison, not a table."

    appended = append_review_section_blocks_to_word_document(
        SimpleNamespace(logger=None),
        1,
        "Results",
        [{
            "block_id": "s1_b1",
            "block_kind": "paragraph",
            "text": prose,
        }],
        str(output_path),
        citation_manifest={},
    )

    assert appended is True
    document = Document(str(output_path))
    assert document.tables == []
    assert any(paragraph.text == prose for paragraph in document.paragraphs)


def test_same_reference_keeps_block_and_cell_specific_modes_and_locators(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "table-citation-modes.docx"
    manifest = _manifest()
    manifest["occurrences"] = [
        {"ref_id": "R006", "paper_id": "10.1/a", "section_number": 1,
         "block_id": "s1_b1", "mode": "narrative", "locator": "p. 1"},
        {"ref_id": "R006", "paper_id": "10.1/a", "section_number": 1,
         "block_id": "s1_b2", "mode": "parenthetical", "locator": "p. 2"},
        {"ref_id": "R006", "paper_id": "10.1/a", "section_number": 1,
         "block_id": "s1_b2", "mode": "narrative", "locator": "p. 3"},
    ]
    appended = append_review_section_blocks_to_word_document(
        SimpleNamespace(logger=None),
        1,
        "Results",
        [
            {"block_id": "s1_b1", "block_kind": "paragraph",
             "text": "Opening claim [[cite_ref:R006]]."},
            {"block_id": "s1_b2", "block_kind": "paragraph",
             "text": "| Item | Finding |\n| --- | --- |\n"
                     "| First | [[cite_ref:R006]] |\n"
                     "| Second | [[cite_ref:R006]] |"},
        ],
        str(output_path),
        citation_manifest=manifest,
    )

    assert appended is True
    document = Document(str(output_path))
    assert any("Chen (2024, p. 1)" in paragraph.text for paragraph in document.paragraphs)
    assert "(Chen, 2024, p. 2)" in document.tables[0].cell(1, 1).text
    assert "Chen (2024, p. 3)" in document.tables[0].cell(2, 1).text

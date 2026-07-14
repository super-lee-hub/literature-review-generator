from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import main
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from config_loader import ConfigDict
from services.progress_state import ResumeStateReport
from summary_schema import normalize_ai_summary


class DummyLogger:
    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def success(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warn(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def make_resume_report(workspace: Any) -> ResumeStateReport:
    return ResumeStateReport(
        artifact_type="resume_state_report",
        artifact_version="v1",
        created_from_job_id=workspace.job_id,
        created_at="2026-04-22T00:00:00Z",
        project_name=workspace.project_name,
        job_id=workspace.job_id,
        state="non_resumable",
        reason="test bootstrap",
        summary_file=workspace.artifact_path(f"{workspace.project_name}_summaries.json"),
        progress_snapshot_file=None,
        checkpoint_file=workspace.checkpoint_path(f"{workspace.project_name}_checkpoint.json"),
        fingerprint_bundle={"request": "demo"},
    )


class BootstrapGenerator(main.LiteratureReviewGenerator):
    def __init__(
        self,
        config_file: str,
        project_name: str,
        pdf_folder: str | None,
        queue_file: str,
        zotero_report: str | None,
        library_path: str | None,
    ) -> None:
        super().__init__(config_file, project_name, pdf_folder, queue_file, zotero_report, library_path)
        self.logger = cast(main.CustomLogger, DummyLogger())

    def load_configuration(self) -> bool:
        output_dir = str(Path(self.queue_file).resolve().parent.parent.parent / "output")
        self.config = ConfigDict(
            {
                "Paths": {"output_path": output_dir},
                "Writer_API": {
                    "api_key": "writer-key",
                    "model": "writer-model",
                    "api_base": "https://example.com/v1",
                },
                "Outline_API": {
                    "api_key": "outline-key",
                    "model": "outline-model",
                    "api_base": "https://example.com/v1",
                },
                "Validator_API": {
                    "api_key": "validator-key",
                    "model": "validator-model",
                    "api_base": "https://example.com/v1",
                },
                "Validation": {"stage1_enabled": "false", "stage2_enabled": "true"},
                "Performance": {"enable_stage2_validation": "true"},
                "Styling": {
                    "font_name": "Times New Roman",
                    "font_size_body": "12",
                    "font_size_heading1": "16",
                    "font_size_heading2": "14",
                },
                "API_Parameters": {
                    "validator_max_tokens": "512",
                    "validator_temperature": "0.0",
                },
            }
        )
        return True


def build_legacy_main() -> Any:
    return SimpleNamespace(LiteratureReviewGenerator=BootstrapGenerator)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_bridge_session(tmp_path: Path, *, action: str = "run_all") -> tuple[AgentRuntimeBridge, Any, Path, Path]:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "alpha.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%alpha\n")

    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)

    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="demo-ai",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action=action,
            queue_file=str(queue_file),
        )
    )
    return bridge, bridge.bootstrap(build_legacy_main()), pdf_dir, pdf_path


def build_success_summary(pdf_path: Path, *, paper_key: str = "paper_a") -> dict[str, Any]:
    from services.artifact_registry import file_sha256

    pdf_path_str = str(pdf_path)
    preprocess_dir = pdf_path.parent / f"{paper_key}_preprocess"
    preprocess_dir.mkdir(exist_ok=True)
    markdown_path = preprocess_dir / "normalized.md"
    chunks_path = preprocess_dir / "chunks.json"
    page_index_path = preprocess_dir / "page_index.json"
    markdown_path.write_text("Paper A source evidence.", encoding="utf-8")
    chunks_path.write_text('[{"chunk_id":"c1","text":"Paper A source evidence."}]', encoding="utf-8")
    page_index_path.write_text('[{"page_number":1,"text":"Paper A source evidence."}]', encoding="utf-8")
    return {
        "status": "success",
        "paper_info": {
            "title": "Paper A",
            "authors": ["Alice Smith"],
            "year": "2024",
            "journal": "Journal of Tests",
            "doi": "10.1000/test.paper",
            "pdf_path": pdf_path_str,
            "canonical_paper_key": paper_key,
            "source_paper_id": pdf_path_str,
            "source_mode": "direct",
            "source_pdf": pdf_path_str,
            "source_pdf_fingerprint": file_sha256(pdf_path),
        },
        "ai_summary": normalize_ai_summary(
            {
                "paper_metadata": {
                    "title": "Paper A",
                    "authors": ["Alice Smith"],
                    "year": "2024",
                    "journal": "Journal of Tests",
                    "doi": "10.1000/test.paper",
                },
                "core_analysis": {
                    "summary": "Paper A reports source-grounded evidence.",
                    "methodology": "Fixture analysis.",
                    "findings": "The fixture contains a deterministic result.",
                    "conclusions": "The deterministic result supports runtime testing.",
                },
            }
        ),
        "stage1_input": {"input_mode": "text", "selected_visual_refs": [], "multimodal_capability": {}},
        "text_length": 1200,
        "processing_time": "1.2",
        "preprocess": {
            "markdown_path": str(markdown_path),
            "chunks_path": str(chunks_path),
            "page_index_path": str(page_index_path),
        },
    }

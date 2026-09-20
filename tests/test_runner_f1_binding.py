from __future__ import annotations

import hashlib
import json
from pathlib import Path

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner


def _write_config(path: Path, output_path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "[Application]",
                "config_schema = 4",
                "",
                "[Paths]",
                f"output_path = {output_path}",
                "",
                "[Primary_Reader_API]",
                "api_key = test-provider-key",
                "model = deepseek-v4-flash",
                "api_base = https://api.deepseek.com",
                "endpoint_type = chat_completions",
                "provider_family = deepseek",
                "",
                "[Stage1_Input]",
                "primary_reader_only = true",
                "",
                "[Validation]",
                "review_enabled = false",
            )
        ),
        encoding="utf-8",
    )


def _write_manifest(source_root: Path) -> tuple[Path, list[dict[str, object]]]:
    sources: list[dict[str, object]] = []
    for index in range(15):
        filename = f"paper-{index + 1:02d}.pdf"
        content = b"%PDF-1.4\n" + f"manifest source {index + 1}".encode("ascii")
        (source_root / filename).write_bytes(content)
        sources.append(
            {
                "source_id": f"f1-{index + 1:02d}",
                "relative_path": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    canonical = {
        "artifact_type": "f1_corpus_manifest",
        "artifact_version": "v1",
        "schema_version": "f1-corpus-manifest-v1",
        "corpus_id": "f1-runner-test",
        "sources": sources,
    }
    payload = {
        **canonical,
        "source_root": str(source_root),
        "content_sha256": hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    manifest_path = source_root.parent / "f1-manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path, sources


def test_runner_rejects_f1_binding_before_stage1_preprocess(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_root = tmp_path / "manifest-sources"
    manifest_root.mkdir()
    manifest_path, sources = _write_manifest(manifest_root)
    runtime_sources = tmp_path / "runtime-sources"
    runtime_sources.mkdir()
    # Deliberately omit one manifest source while retaining valid independent
    # PDFs. The runner must fail during source intake before Stage 1 begins.
    for source in sources[:-1]:
        filename = str(source["relative_path"])
        (runtime_sources / filename).write_bytes((manifest_root / filename).read_bytes())

    config_path = tmp_path / "config.ini"
    _write_config(config_path, tmp_path / "output")
    stage1_called = False

    def forbidden_stage1(*_args, **_kwargs):
        nonlocal stage1_called
        stage1_called = True
        raise AssertionError("Stage 1 must not start after F1 source binding rejection")

    monkeypatch.setattr(
        "services.stage1_analysis_service.Stage1AnalysisService.run",
        forbidden_stage1,
    )
    spec = RuntimeJobSpec(
        project_name="f1-runner-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(runtime_sources)),
        job_id="f1-runner-test-job",
        config=str(config_path),
        action="analyze",
        metadata={
            "f1_corpus_binding": {
                "schema_version": "f1-corpus-binding-v1",
                "manifest_path": str(manifest_path),
                "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "source_ids": [str(source["source_id"]) for source in sources],
            }
        },
    )

    result = AgentRuntimeRunner(spec).run()

    assert result.job_status == "failed"
    assert result.failed_stage == "source_intake"
    assert stage1_called is False

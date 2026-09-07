from __future__ import annotations

import configparser
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import fitz  # type: ignore

from config_loader import load_config
from free_mode.profile_manager import get_profile_path, save_profile
from runtime.job_spec import RuntimeJobSpec
from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderBudgetExceeded,
    ProviderBudgetController,
    ProviderRuntime,
)
from runtime.release_acceptance import ReleaseAcceptanceSpec, ReleaseAcceptanceSpecError
from runtime.zotero_attachment_resolver import (
    ZoteroAttachmentIndex,
    ZoteroAttachmentResolutionError,
)
from services.job_workspace import JobWorkspace, WorkspacePathError
from preprocess.service import MineruArtifactLimitError, PreprocessManager
from validation.evidence_loader import (
    PreprocessEvidenceLoader,
    ValidationSourceAuthorityError,
)


def _write_config(path: Path, values: dict[str, dict[str, str]]) -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    for section, items in values.items():
        parser[section] = items
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _runtime_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_name": "demo",
        "source": {"mode": "direct", "pdf_folder": "papers"},
    }
    payload.update(overrides)
    return payload


def test_runtime_job_spec_rejects_unknown_top_level_and_nested_keys() -> None:
    with pytest.raises(ValueError, match="pdf_fodler"):
        RuntimeJobSpec.from_dict(_runtime_payload(pdf_fodler="papers"))
    with pytest.raises(ValueError, match="launch-missiles"):
        RuntimeJobSpec.from_dict(
            _runtime_payload(metadata={"launch-missiles": True})
        )
    with pytest.raises(ValueError, match="source.*unknown"):
        RuntimeJobSpec.from_dict(
            _runtime_payload(source={"mode": "direct", "pdf_folder": "papers", "unknown": 1})
        )


def test_stage_plan_config_admission_does_not_require_unreachable_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "minimal.ini"
    _write_config(
        config_path,
        {
            "Application": {"config_schema": "4"},
            "Paths": {"output_path": str(tmp_path / "output")},
            "Primary_Reader_API": {
                "api_key": "sk-primary-reader",
                "model": "deepseek-v4-pro",
                "api_base": "https://api.deepseek.com",
                "endpoint_type": "chat_completions",
                "provider_family": "deepseek",
            },
            "Stage1_Input": {"primary_reader_only": "true"},
        },
    )

    loaded = load_config(
        str(config_path),
        action="analyze",
        requested_stages=("analyze",),
    )

    assert "Backup_Reader_API" not in loaded
    assert "Writer_API" not in loaded
    assert "Outline_API" not in loaded
    assert "OutlineModels" not in loaded


def test_stage_plan_config_admission_requires_reachable_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "needs-backup.ini"
    _write_config(
        config_path,
        {
            "Application": {"config_schema": "4"},
            "Paths": {"output_path": str(tmp_path / "output")},
            "Primary_Reader_API": {
                "api_key": "sk-primary-reader",
                "model": "deepseek-v4-pro",
                "api_base": "https://api.deepseek.com",
                "endpoint_type": "chat_completions",
                "provider_family": "deepseek",
            },
            "Stage1_Input": {"primary_reader_only": "false"},
        },
    )

    with pytest.raises(configparser.Error, match="Backup_Reader_API"):
        load_config(str(config_path), action="analyze", requested_stages=("analyze",))


def test_specialized_gate_never_passes_from_completed_job_alone(tmp_path: Path, monkeypatch) -> None:
    from scripts import release_acceptance

    spec = tmp_path / "runtime.json"
    spec.write_text(json.dumps(_runtime_payload()), encoding="utf-8")
    args = SimpleNamespace(
        timeout_seconds=30,
        max_provider_calls=24,
        max_output_tokens=5000000,
        max_retry_attempts=2,
    )
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    monkeypatch.setattr(
        release_acceptance,
        "_command",
        lambda *_args, **_kwargs: {
            "status": "PASS",
            "job_status": "completed",
            "completion_status": "complete",
        },
    )

    result = release_acceptance._live_gate(
        tmp_path,
        gate="I",
        spec=spec,
        args=args,
        preflight={"status": "PASS"},
    )

    assert result["status"] != "PASS"
    assert "evidence" in str(result.get("reason", "")).lower() or result["status"] == "NOT_VERIFIED"


def test_release_acceptance_budget_schema_rejects_typos() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="max_provider_call"):
        ReleaseAcceptanceSpec.from_mapping(
            {"budget": {"max_provider_call": 1}}
        )


def test_aggregate_provider_budget_is_shared_and_reserves_transport_attempts() -> None:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=2,
            max_output_tokens_total=8,
            max_retry_attempts_total=1,
            max_wall_seconds=60,
        )
    )
    first = ProviderRuntime(
        aggregate_budget=controller,
        test_only=True,
    )
    second = ProviderRuntime(
        aggregate_budget=controller,
        test_only=True,
    )

    admission = first.admit(
        estimated_tokens=1,
        requested_output_tokens=4,
        requested_retry_attempts=1,
    )
    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        second.admit(
            estimated_tokens=1,
            requested_output_tokens=1,
            requested_retry_attempts=0,
        )

    first.complete(
        admission=admission,
        prompt="prompt",
        input_payload={"text": "input"},
        api_config={"model": "model", "api_base": "https://example.test"},
        result={"status": "success", "content": {}, "attempts": 2, "output_tokens": 3},
    )
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 2
    assert snapshot["output_tokens_used"] == 3
    assert snapshot["retry_attempts_used"] == 1


def test_acceptance_budget_environment_binds_all_provider_runtimes(monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        json.dumps(
            {
                "max_provider_calls_total": 1,
                "max_output_tokens_total": 4,
                "max_retry_attempts_total": 0,
                "max_wall_seconds": 60,
            }
        ),
    )
    first = ProviderRuntime(test_only=True)
    second = ProviderRuntime(test_only=True)
    first.admit(estimated_tokens=1, requested_output_tokens=4)
    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        second.admit(estimated_tokens=1, requested_output_tokens=1)


def test_aggregate_provider_budget_state_survives_process_boundary(tmp_path: Path) -> None:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=2,
        max_output_tokens_total=8,
        max_retry_attempts_total=1,
        max_wall_seconds=60,
    )
    state_path = tmp_path / "budget-state.json"
    first_controller = ProviderBudgetController(budget)
    first_controller.bind_state_path(state_path)
    first_runtime = ProviderRuntime(aggregate_budget=first_controller, test_only=True)
    admission = first_runtime.admit(
        estimated_tokens=1,
        requested_output_tokens=4,
        requested_retry_attempts=0,
    )
    first_runtime.complete(
        admission=admission,
        prompt="prompt",
        input_payload={"text": "input"},
        api_config={"model": "model", "api_base": "https://example.test"},
        result={"status": "success", "content": {}, "attempts": 1, "output_tokens": 3},
    )

    second_controller = ProviderBudgetController(budget)
    second_controller.bind_state_path(state_path)
    assert second_controller.snapshot()["calls_used"] == 1
    second_runtime = ProviderRuntime(aggregate_budget=second_controller, test_only=True)
    second_runtime.admit(estimated_tokens=1, requested_output_tokens=5)
    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        ProviderRuntime(aggregate_budget=second_controller, test_only=True).admit(
            estimated_tokens=1,
            requested_output_tokens=1,
        )


def test_profile_path_rejects_traversal_and_save_is_atomic_boundary(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError):
        get_profile_path(str(tmp_path), "../escape")
    with pytest.raises(WorkspacePathError):
        save_profile({"research_goal": "x"}, str(tmp_path), "CON")

    path = Path(save_profile({"research_goal": "x"}, str(tmp_path), "safe"))
    assert path == Path(get_profile_path(str(tmp_path), "safe"))
    assert json.loads(path.read_text(encoding="utf-8"))["research_goal"] == "x"


@pytest.mark.optional
def test_workspace_rejects_existing_reparse_leaf_for_production_artifact_path(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "project", "job")
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    link = Path(workspace.paths.artifacts_dir) / "link.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable in this environment")
        raise

    with pytest.raises(WorkspacePathError, match="reparse|symlink"):
        workspace.artifact_path("link.json")


def test_strict_evidence_loader_rejects_tampered_required_artifact(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.md"
    chunks = tmp_path / "chunks.json"
    page_index = tmp_path / "page_index.json"
    manifest = tmp_path / "manifest.json"
    normalized.write_text("authoritative text", encoding="utf-8")
    chunks.write_text("[]", encoding="utf-8")
    page_index.write_text("[]", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "artifact_hashes": {
                    "normalized.md": hashlib.sha256(normalized.read_bytes()).hexdigest(),
                    "chunks.json": hashlib.sha256(chunks.read_bytes()).hexdigest(),
                    "page_index.json": hashlib.sha256(page_index.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    loader = PreprocessEvidenceLoader()
    loader.load_evidence(
        normalized_text_path=str(normalized),
        chunks_path=str(chunks),
        page_index_path=str(page_index),
        manifest_path=str(manifest),
        strict=True,
    )

    normalized.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValidationSourceAuthorityError, match="hash"):
        loader.load_evidence(
            normalized_text_path=str(normalized),
            chunks_path=str(chunks),
            page_index_path=str(page_index),
            manifest_path=str(manifest),
            strict=True,
        )


def test_preprocess_cache_binds_source_hash_fingerprint_and_atomic_generation(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "cache identity test\n" * 20)
    document.save(pdf)
    document.close()
    manager = PreprocessManager(
        config={
            "Paths": {"output_path": str(tmp_path)},
            "Preprocess": {
                "enabled": "true",
                "cache_dir": str(tmp_path / "cache"),
                "extractor_profile": "fitz",
                "ocr_mode": "off",
            },
        }
    )
    first = manager.prepare_pdf(str(pdf))
    assert first is not None
    manifest = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))
    assert len(manifest["source_pdf_sha256"]) == 64
    assert manifest["processing_fingerprint"] == manager.processing_fingerprint
    assert manifest["artifact_hashes"]["normalized.md"]["sha256"]

    pointer = Path(first.cache_dir) / "active_generation.json"
    old_pointer = pointer.read_bytes()
    manager.force_rebuild = True

    def crash_after_staging(*_args, **_kwargs):
        raise RuntimeError("controlled generation crash")

    monkeypatch.setattr(manager, "_write_json_durable", crash_after_staging)
    with pytest.raises(RuntimeError, match="controlled generation crash"):
        manager.prepare_pdf(str(pdf))
    assert pointer.read_bytes() == old_pointer
    assert Path(first.manifest_path).is_file()


def test_mineru_rejects_oversized_source_before_upload(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "oversized.pdf"
    pdf.write_bytes(b"12345")
    monkeypatch.setenv("MINERU_API_TOKEN", "token")
    manager = PreprocessManager(
        config={
            "Preprocess": {"source_pdf_max_bytes": "4"},
        }
    )
    with pytest.raises(MineruArtifactLimitError, match="source PDF"):
        manager._extract_with_mineru_remote(str(pdf), [], [], [])


def test_zotero_managed_storage_is_contained_but_linked_files_can_be_external(tmp_path: Path) -> None:
    storage = tmp_path / "zotero" / "storage"
    managed_root = storage / "KEY123"
    managed_root.mkdir(parents=True)
    managed = managed_root / "paper.pdf"
    managed.write_bytes(b"pdf")
    linked = tmp_path / "external" / "paper.pdf"
    linked.parent.mkdir()
    linked.write_bytes(b"pdf")
    index = object.__new__(ZoteroAttachmentIndex)
    index.storage_root = storage
    index.zotero_root = storage.parent

    assert index._resolve_attachment_path("storage:paper.pdf", "KEY123", 0) == managed.resolve()
    with pytest.raises(ZoteroAttachmentResolutionError):
        index._resolve_attachment_path("storage:../../outside.pdf", "KEY123", 0)
    assert index._resolve_attachment_path(str(linked), "LINKED1", 2) == linked.resolve()

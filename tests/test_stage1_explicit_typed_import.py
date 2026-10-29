"""Fast controls for the explicit typed Stage 1 import boundary.

The positive rows in these tests come from a real Stage 1 generation and its
published typed manifest.  The helper is therefore exercised against genuine
authority bytes rather than the hand-made authority fixture used by the
current-boundary unit tests.  The injected reader is deliberately local and
only supplies the generated summary; these tests do not claim provider
transport acceptance.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import InternalStageExecutorRegistry
from runtime.stage_contracts import SourceBundle, build_source_bundle
from services.explicit_stage1_typed_import import (
    ExplicitTypedImportError,
    resolve_explicit_typed_imports,
)

from tests.test_current_stage1_generation import (
    _canonical_summary,
    _service,
    _typed_manifest_record,
    _write_pdf,
)


@pytest.mark.unit
def test_stage_host_accepts_normalized_null_runtime_metadata(tmp_path: Path) -> None:
    from runtime.orchestrator import _RuntimeStageHost
    from services.queue_service import CancelToken
    from tests.test_runtime_bridge_helpers import current_config

    spec = RuntimeJobSpec(
        project_name="null-metadata",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
        action="analyze",
        reuse_stage1=False,
        config=str(current_config(tmp_path)),
    )
    payload = spec.to_dict()
    payload["metadata"] = None
    request = replace(spec.to_job_request(), runtime_spec_payload=payload)
    host = _RuntimeStageHost(request, spec.project_name, CancelToken())
    assert host.project_name == spec.project_name
    assert host.reuse_stage1 is False


def _second_bundle_item(tmp_path: Path) -> Any:
    second_pdf = tmp_path / "second.pdf"
    _write_pdf(second_pdf)
    return build_source_bundle(
        source_mode="direct",
        project_name="explicit-import",
        papers=[
            {
                "title": "Second source paper",
                "authors": ["Second Author"],
                "year": "2024",
                "doi": "10.1000/second",
                "pdf_path": str(second_pdf),
                "source_paper_id": "paper-2",
            }
        ],
    ).paper_work_items[0]


def _full_bundle(parent_bundle: SourceBundle, tmp_path: Path) -> SourceBundle:
    """Keep the generated first item and add one current pending PDF."""

    return SourceBundle(
        source_mode=parent_bundle.source_mode,
        project_name="explicit-import",
        paper_work_items=[parent_bundle.paper_work_items[0], _second_bundle_item(tmp_path)],
        source_snapshot={"fixture": "explicit-typed-import"},
    )


def _generated_import_fixture(tmp_path: Path) -> tuple[dict[str, Any], SourceBundle, Any]:
    """Return a genuine generated typed row, a two-paper bundle, and owner service."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    source_pdf = tmp_path / "generated.pdf"
    _write_pdf(source_pdf)

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    owner, owner_bundle = _service(tmp_path / "owner", source_pdf, reader)
    generated = owner.run(owner_bundle)
    assert generated.generated_count == 1
    manifest = _typed_manifest_record(owner)
    loaded_rows = InternalStageExecutorRegistry._summary_payloads_from_file(manifest.path)
    assert len(loaded_rows) == 1
    row = dict(loaded_rows[0])
    assert row["stage1_reuse"]["authority_kind"] == "typed_manifest"
    return row, _full_bundle(owner_bundle, tmp_path), owner


def _owner_resolver(owner: Any):
    registry_id = f"artifact-registry:{owner.job_id}"
    return lambda requested: owner.registry if requested == registry_id else None


@pytest.mark.unit
def test_explicit_typed_import_freezes_real_authority_and_current_pdf_join(
    tmp_path: Path,
) -> None:
    row, full_bundle, owner = _generated_import_fixture(tmp_path)

    plan = resolve_explicit_typed_imports(
        [row],
        full_bundle,
        external_registry_resolver=_owner_resolver(owner),
    )

    assert tuple(entry.paper_key for entry in plan.imports) == (
        full_bundle.paper_work_items[0].canonical_paper_key,
    )
    assert plan.pending_keys == (full_bundle.paper_work_items[1].canonical_paper_key,)
    assert len(plan.authority_set_hash) == 64
    imported = plan.imports[0]
    assert imported.authority.manifest.canonical_paper_key == imported.paper_key
    assert imported.summary == row
    assert imported.summary is not row
    assert imported.summary["stage1_reuse"]["binding"] == row["stage1_reuse"]["binding"]
    assert imported.source_item()["disposition"] == "typed_imported"
    assert imported.source_item()["current_transport_count"] == 0
    source_item = imported.source_item()
    assert source_item["current_pdf_join"] == {
        "canonical_paper_key": imported.paper_key,
        "source_paper_id": full_bundle.paper_work_items[0].source_paper_id,
        "path": str(Path(full_bundle.paper_work_items[0].source_pdf).resolve()),
        "sha256": source_item["source_pdf_sha256"],
    }
    assert imported.identity()["typed_manifest_artifact_hash"]

    # The plan is frozen and exposes a reconstructed full bundle.  Mutating a
    # caller-owned row cannot alter the imported authority or pending set.
    row["ai_summary"]["core_analysis"]["summary"] = "caller mutation"
    assert plan.imports[0].summary["ai_summary"]["core_analysis"]["summary"] != (
        "caller mutation"
    )
    assert tuple(item.canonical_paper_key for item in full_bundle.paper_work_items) == (
        imported.paper_key,
        full_bundle.paper_work_items[1].canonical_paper_key,
    )


@pytest.mark.unit
def test_explicit_import_preserves_raw_summary_and_historical_provider_origin(
    tmp_path: Path,
) -> None:
    row, full_bundle, owner = _generated_import_fixture(tmp_path)
    original_provider = copy.deepcopy(row.get("provider"))
    original_binding = copy.deepcopy(row["stage1_reuse"]["binding"])

    plan = resolve_explicit_typed_imports(
        [row],
        full_bundle,
        external_registry_resolver=_owner_resolver(owner),
    )
    source_item = plan.imports[0].source_item()
    assert plan.imports[0].summary["provider"] == original_provider
    assert plan.imports[0].summary["stage1_reuse"]["binding"] == original_binding
    assert source_item["current_transport_count"] == 0


@pytest.mark.unit
def test_explicit_import_rejects_invalid_authority_pdf_visual_key_and_duplicates(
    tmp_path: Path,
) -> None:
    row, full_bundle, owner = _generated_import_fixture(tmp_path)
    cases: list[tuple[str, dict[str, Any], str]] = []

    invalid_authority = copy.deepcopy(row)
    invalid_authority["stage1_reuse"]["typed_manifest_artifact_hash"] = "0" * 64
    cases.append(("authority", invalid_authority, "typed_manifest_file_hash_mismatch"))

    invalid_pdf = copy.deepcopy(row)
    invalid_pdf["stage1_reuse"]["binding"]["source_pdf_content_sha256"] = "1" * 64
    cases.append(("pdf", invalid_pdf, "source_pdf_content_sha256_mismatch"))

    invalid_visual = copy.deepcopy(row)
    qualification = invalid_visual["stage1_reuse"]["binding"].setdefault(
        "visual_evidence_qualification", {}
    )
    qualification["evidence_coverage_status"] = "incomplete"
    cases.append(("visual", invalid_visual, "typed_manifest_imported_binding_mismatch"))

    invalid_key = copy.deepcopy(row)
    invalid_key["paper_info"]["canonical_paper_key"] = "unknown-paper"
    cases.append(("key", invalid_key, "unknown_canonical_paper_key"))

    for _label, candidate, reason in cases:
        with pytest.raises(ExplicitTypedImportError, match=reason):
            resolve_explicit_typed_imports(
                [candidate],
                full_bundle,
                external_registry_resolver=_owner_resolver(owner),
            )

    duplicate = [copy.deepcopy(row), copy.deepcopy(row)]
    with pytest.raises(ExplicitTypedImportError, match="duplicate_explicit_row"):
        resolve_explicit_typed_imports(
            duplicate,
            full_bundle,
            external_registry_resolver=_owner_resolver(owner),
        )


@pytest.mark.unit
def test_runtime_spec_gate_requires_explicit_mode_reuse_and_typed_manifest_paths(
    tmp_path: Path,
) -> None:
    source = RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path / "pdfs"))
    with pytest.raises(ValueError, match="reuse_stage1=true"):
        RuntimeJobSpec(
            project_name="explicit-gate",
            source=source,
            action="analyze",
            metadata={"stage1_summary_input_mode": "explicit_typed_import"},
        ).validate()

    with pytest.raises(ValueError, match="explicit typed Stage 1 import"):
        RuntimeJobSpec(
            project_name="explicit-gate",
            source=source,
            action="generate_outline",
            reuse_stage1=True,
            reuse_summary_files=(str(tmp_path / "manifest.json"),),
            metadata={"stage1_summary_input_mode": "explicit_typed_import"},
        ).validate()

    with pytest.raises(ValueError, match="requires Analyze in requested_stages"):
        RuntimeJobSpec(
            project_name="explicit-gate",
            source=source,
            action="run_all",
            reuse_stage1=True,
            reuse_summary_files=(str(tmp_path / "manifest.json"),),
            metadata={
                "stage1_summary_input_mode": "explicit_typed_import",
                "requested_stages": ["source_intake", "outline"],
            },
        ).validate()

    spec = RuntimeJobSpec(
        project_name="explicit-gate",
        source=source,
        action="analyze",
        reuse_stage1=True,
        reuse_summary_files=(str(tmp_path / "manifest.json"),),
        metadata={"stage1_summary_input_mode": "explicit_typed_import"},
    )
    spec.validate()


@pytest.mark.unit
def test_ordinary_existing_summary_reuse_still_invalidates_changed_reader_config(
    tmp_path: Path,
) -> None:
    row, _full_bundle_value, _owner = _generated_import_fixture(tmp_path / "ordinary")
    source_pdf = Path(str(row["paper_info"]["source_pdf"]))
    calls: list[int] = []

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    changed, changed_bundle = _service(
        tmp_path / "ordinary" / "changed",
        source_pdf,
        reader,
        job_id="ordinary-cache-changed",
        config_overrides={"Primary_Reader_API": {"model": "reader-changed"}},
    )
    result = changed.run(changed_bundle, existing_summaries=[row])

    assert result.reused_count == 0
    assert result.generated_count == 1
    assert result.expected_provider_transport_count == 1
    assert result.actual_provider_transport_count == 1
    assert len(calls) == 1


@pytest.mark.unit
def test_direct_service_explicit_argument_requires_registered_runtime_spec(
    tmp_path: Path,
) -> None:
    row, full_bundle, _owner = _generated_import_fixture(tmp_path)
    calls: list[int] = []

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    source_pdf = Path(str(row["paper_info"]["source_pdf"]))
    service, _bundle = _service(tmp_path / "direct-service", source_pdf, reader)
    with pytest.raises(
        RuntimeError,
        match="requires the registered current RuntimeJobSpec",
    ):
        service.run(full_bundle, explicit_typed_imports=[row])
    assert calls == []


@pytest.mark.unit
def test_explicit_import_rejects_conflicting_secondary_pdf_hash_declarations(
    tmp_path: Path,
) -> None:
    row, full_bundle, owner = _generated_import_fixture(tmp_path)

    row_level = copy.deepcopy(row)
    row_level["source_pdf_content_sha256"] = "0" * 64
    with pytest.raises(ExplicitTypedImportError, match="source_pdf"):
        resolve_explicit_typed_imports(
            [row_level],
            full_bundle,
            external_registry_resolver=_owner_resolver(owner),
        )

    secondary_binding = copy.deepcopy(row)
    binding = secondary_binding["stage1_reuse"]["binding"]
    binding.setdefault("extra", {})["source_pdf_file_hash"] = "1" * 64
    with pytest.raises(ExplicitTypedImportError, match="source_pdf"):
        resolve_explicit_typed_imports(
            [secondary_binding],
            full_bundle,
            external_registry_resolver=_owner_resolver(owner),
        )


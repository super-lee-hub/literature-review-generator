"""Admission component checks; full canonical parent acceptance is an E2E gate."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import fitz
import pytest

import runtime.source_revision_admission as admission
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_response_snapshot import ProviderResponseSnapshotError, ProviderResponseSnapshotStore
from runtime.provider_runtime import ProviderAggregateBudgetV2, hash_json
from runtime.source_intake import build_direct_source_bundle
from runtime.source_revision import SourceRevisionError, child_core_spec_hash, classify_replacement_delta
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, UnverifiedArtifact, file_sha256
from services.job_workspace import JobWorkspace
from services.paper_identity import build_canonical_paper_key
from services.queue_service import LocalPublicationContext
from services.review_batch import ReviewBatchSpecV1, SummarySelectionSpecV1
from tests.test_provider_response_snapshot import _successful_receipt


def _pdf(path: Path, title: str, doi: str, qualifier: str = "Only adults.") -> None:
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), f"{title}\nResearcher Example\n{doi}\n{qualifier}")
        document.set_metadata({"title": title, "author": "Researcher Example", "subject": doi})
        document.save(str(path))


def _corpora(tmp_path: Path):
    old = tmp_path / "original"
    new = tmp_path / "revised"
    old.mkdir()
    new.mkdir()
    _pdf(old / "a.pdf", "Controlled source A", "10.1234/source-a")
    _pdf(old / "b.pdf", "Controlled source B", "10.1234/source-b")
    shutil.copyfile(old / "a.pdf", new / "a.pdf")
    _pdf(new / "b.pdf", "Controlled source B", "10.1234/source-b", "Only adults over age fifty.")
    return (build_direct_source_bundle(project_name="revision", pdf_folder=str(old)),
            build_direct_source_bundle(project_name="revision", pdf_folder=str(new)))


def _spec(tmp_path: Path) -> RuntimeJobSpec:
    return RuntimeJobSpec(
        project_name="revision", job_id="new-child", action="run_all",
        config=str(tmp_path / "config.ini"), workspace_path=str(tmp_path / "output" / "revision__new-child"),
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path / "revised")),
        metadata={"provider_semantic_contract_version": admission.PROVIDER_SEMANTIC_CONTRACT_V2,
                  "provider_budget": ProviderAggregateBudgetV2(20, 10000, 0, 600).to_dict(),
                  "review_intent": {"question": "Keep source qualifiers"}},
    )


def _register(registry, workspace, artifact_id, artifact_type, payload, depends_on=()):
    path = Path(workspace.artifact_path(artifact_id.replace(":", "-") + ".json"))
    path.write_text(json.dumps(payload), encoding="utf-8")
    return registry.register_file(artifact_id=artifact_id, artifact_role=artifact_type,
                                  artifact_type=artifact_type, artifact_version="v1", path=path,
                                  producer="tests.source_revision_admission", depends_on=depends_on)


def _body(tmp_path):
    old_bundle, new_bundle = _corpora(tmp_path)
    old = admission._source_rows(old_bundle)
    new = admission._source_rows(new_bundle, original=old_bundle)
    spec = _spec(tmp_path)
    return {
        "schema_version": admission.PLAN_VERSION, "mode": admission.PLAN_MODE,
        "provider_semantic_contract_version": admission.PROVIDER_SEMANTIC_CONTRACT_V2,
        "child_core_spec_hash": child_core_spec_hash(spec.to_dict()), "child_job_id": spec.job_id,
        "child_project_name": spec.project_name, "child_workspace_path": spec.workspace_path,
        "parent": {"workspace_path": str(tmp_path / "output" / "revision__parent"), "job_id": "parent",
                   "registry_path": str(tmp_path / "output" / "revision__parent" / "artifact_registry.json"),
                   "authority_refs": [], "response_snapshot_refs": []},
        "old_sources": old, "new_sources": new, "changed_paper_key": new[1]["canonical_paper_key"],
        "unchanged_paper_keys": [new[0]["canonical_paper_key"]], "review_intent_hash": hash_json(spec.metadata["review_intent"]),
        "config_binding": {"parent_path": spec.config, "child_path": spec.config, "file_sha256": "a" * 64},
        "provider_budget": spec.metadata["provider_budget"],
    }, spec


def _bind(tmp_path, spec, body):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return replace(spec, metadata={**spec.metadata, "source_revision_plan": {
        "schema_version": "source-revision-plan-ref/v1", "path": str(path),
        "file_sha256": file_sha256(path), "content_hash": hash_json(body),
    }})


def test_real_direct_pdf_identity_allows_relocation_and_one_qualifier_change(tmp_path):
    old, new = _corpora(tmp_path)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    old_rows = admission._source_rows(old)
    new_rows = admission._source_rows(new, original=old)
    project = lambda rows: [{key: row[key] for key in ("canonical_paper_key", "source_pdf", "pdf_sha256")} for row in rows]
    delta = classify_replacement_delta(project(old_rows), project(new_rows))
    assert delta.changed_paper_key == old_rows[1]["canonical_paper_key"]
    assert delta.unchanged_paper_keys == (old_rows[0]["canonical_paper_key"],)
    assert [row["identity_hash"] for row in old_rows] == [row["identity_hash"] for row in new_rows]
    assert all(isinstance(item.source_descriptor["source_pdf_fingerprint"], str) for item in old.paper_work_items)
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("mutation", ["stale_bytes", "wrong_doi", "no_identity", "canonical_key"])
def test_actual_pdf_or_canonical_identity_cannot_be_substituted(tmp_path, mutation):
    old, new = _corpora(tmp_path)
    path = Path(new.paper_work_items[1].source_pdf)
    if mutation == "canonical_key":
        item = replace(new.paper_work_items[1], canonical_paper_key="renamed-key")
        new = replace(new, paper_work_items=[new.paper_work_items[0], item])
    else:
        path.unlink()
        if mutation == "no_identity":
            with fitz.open() as doc:
                doc.new_page().insert_text((72, 72), "A document without independent bibliographic identity.")
                doc.save(str(path))
        else:
            _pdf(path, "Controlled source B", "10.1234/wrong-source" if mutation == "wrong_doi" else "10.1234/source-b")
        if mutation != "stale_bytes":
            new = build_direct_source_bundle(project_name="revision", pdf_folder=str(path.parent))
    with pytest.raises(SourceRevisionError):
        admission._source_rows(new, original=old)


def test_zotero_expected_doi_is_not_replaced_with_observed_wrong_pdf_identity(tmp_path):
    old, _new = _corpora(tmp_path)
    item = old.paper_work_items[0]
    paper = {**item.paper_info, "doi": "10.1234/different-expected-paper"}
    wrong = replace(item, source_mode="zotero", paper_info=paper, canonical_paper_key=build_canonical_paper_key(paper))
    with pytest.raises(SourceRevisionError, match="bibliographic identity"):
        admission._source_rows(replace(old, source_mode="zotero", paper_work_items=[wrong]))


def test_namespace_path_calculation_creates_nothing_and_existing_requires_exact_registered_spec(tmp_path):
    spec = _spec(tmp_path)
    config = {"Paths": {"output_path": str(tmp_path / "output")}}
    assert admission._child_namespace(spec, config, allow_existing=False) == Path(spec.workspace_path)
    assert list(tmp_path.iterdir()) == []
    workspace = JobWorkspace.create(str(tmp_path / "output"), "revision", "new-child")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    with pytest.raises(SourceRevisionError, match="fresh"):
        admission._child_namespace(spec, config, allow_existing=False)
    with pytest.raises(SourceRevisionError, match="registered runtime spec"):
        admission._child_namespace(spec, config, allow_existing=True)
    _register(registry, workspace, "runtime_job_spec", "runtime_job_spec", spec.to_dict())
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert admission._child_namespace(spec, config, allow_existing=True) == Path(spec.workspace_path)
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    with pytest.raises(SourceRevisionError, match="spec/owner differs"):
        admission._child_namespace(replace(spec, metadata={**spec.metadata, "audit_reason": "foreign"}), config, allow_existing=True)


@pytest.mark.parametrize("workspace", ["", "relative", "wrong-layout"])
def test_child_must_bind_actual_explicit_bootstrap_layout(tmp_path, workspace):
    spec = _spec(tmp_path)
    value = str(tmp_path / "other") if workspace == "wrong-layout" else workspace
    with pytest.raises(SourceRevisionError):
        admission._child_namespace(replace(spec, workspace_path=value), {"Paths": {"output_path": str(tmp_path / "output")}}, allow_existing=False)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mutation", ["extra", "missing", "parent_extra", "source_extra", "bad_hash", "path_type", "empty_text", "budget_string", "budget_missing", "budget_zero", "scalar"])
def test_plan_body_schema_is_closed_and_uncoerced(tmp_path, mutation):
    body, _ = _body(tmp_path)
    admission._validate_body(body)
    if mutation == "extra": body["trusted"] = True
    elif mutation == "missing": del body["review_intent_hash"]
    elif mutation == "parent_extra": body["parent"]["status_verified"] = True
    elif mutation == "source_extra": body["new_sources"][0]["verified"] = True
    elif mutation == "bad_hash": body["old_sources"][0]["pdf_sha256"] = "A" * 64
    elif mutation == "path_type": body["parent"]["workspace_path"] = []
    elif mutation == "empty_text": body["child_job_id"] = ""
    elif mutation == "budget_string": body["provider_budget"]["max_provider_calls_total"] = "20"
    elif mutation == "budget_missing": body["provider_budget"] = None
    elif mutation == "budget_zero": body["provider_budget"]["max_wall_seconds"] = 0
    else: body = 42
    with pytest.raises(SourceRevisionError):
        admission._validate_body(body)


@pytest.mark.parametrize("mutation", ["file_bytes", "body_hash", "unknown_field"])
def test_plan_ref_hash_or_schema_failure_precedes_parent_access(tmp_path, monkeypatch, mutation):
    body, spec = _body(tmp_path)
    if mutation == "unknown_field": body["unknown"] = "reject"
    bound = _bind(tmp_path, spec, body)
    if mutation == "file_bytes": Path(bound.metadata["source_revision_plan"]["path"]).write_text("{}", encoding="utf-8")
    elif mutation == "body_hash":
        reference = {**bound.metadata["source_revision_plan"], "content_hash": "f" * 64}
        bound = replace(bound, metadata={**bound.metadata, "source_revision_plan": reference})
    monkeypatch.setattr(admission, "_build", lambda *args, **kwargs: pytest.fail("invalid ref reached parent authority"))
    with pytest.raises(SourceRevisionError):
        admission.verify_source_revision_plan_v1(bound)
    assert not Path(spec.workspace_path).exists()


@pytest.mark.parametrize("value", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_plan_json_duplicate_keys_and_nonfinite_numbers_reject(tmp_path, value):
    path = tmp_path / "plan.json"
    path.write_text(value, encoding="utf-8")
    with pytest.raises(SourceRevisionError): admission._read(path)


@pytest.mark.parametrize("legacy", [False, True])
def test_real_registered_parent_bundle_cannot_masquerade_as_completed_job(tmp_path, legacy):
    from tests.test_current_runtime_full_e2e import _test_config

    old, _new = _corpora(tmp_path)
    config = _test_config(tmp_path)
    spec = replace(_spec(tmp_path), config=str(config))
    loaded_path, loaded = admission._config(spec)
    spec = replace(spec, workspace_path=str(Path(loaded["Paths"]["output_path"]) / "revision__new-child"))
    parent = JobWorkspace.create(loaded["Paths"]["output_path"], "revision", "parent")
    registry = ArtifactRegistry(parent.paths.registry_path, parent.job_id)
    parent_spec = replace(spec, job_id="parent", source=replace(spec.source, pdf_folder=str(tmp_path / "original")), workspace_path=parent.root_dir)
    if legacy:
        parent_spec = replace(parent_spec, metadata={key: value for key, value in parent_spec.metadata.items()
                                                    if key != "provider_semantic_contract_version"})
    _register(registry, parent, "runtime_job_spec", "runtime_job_spec", parent_spec.to_dict())
    _register(registry, parent, "source_bundle", "source_bundle", old.to_dict())
    before = {str(path): path.read_bytes() for path in Path(parent.root_dir).rglob("*") if path.is_file()}
    with pytest.raises(SourceRevisionError, match="legacy parent" if legacy else "completed canonical"):
        admission.build_source_revision_plan_v1(spec, Path(parent.root_dir))
    assert loaded_path == config
    assert not Path(spec.workspace_path).exists()
    assert {str(path): path.read_bytes() for path in Path(parent.root_dir).rglob("*") if path.is_file()} == before


def _captured_graph(tmp_path, *, capture):
    workspace = JobWorkspace.create(str(tmp_path / "output"), "revision", "parent")
    context = LocalPublicationContext()
    registry = context.registry(workspace.paths.registry_path, workspace.job_id)
    receipt, ledger = _successful_receipt(workspace, {"source": "known original"})
    ledger_record = registry.register_file(artifact_id="review:provider_receipt_ledger", artifact_role="provider_receipt_ledger",
        artifact_type="provider_receipt_ledger", artifact_version="v1", path=ledger.path, producer="tests.source_revision_admission")
    capsule = ProviderResponseSnapshotStore(workspace, registry, context).capture(receipt, {"source": "known original"}) if capture else None
    closure_payload = {"artifact_type": "provider_receipt_closure", "artifact_version": "v1", "job_id": workspace.job_id,
        "status": "ready", "payload": {"closure_epoch_id": receipt.closure_epoch_id, "expected_call_ids": [receipt.call_id],
        "observed_call_ids": [receipt.call_id], "missing_call_ids": [], "hash_mismatches": {}, "complete": True,
        "closure_hash": hash_json(receipt.to_dict()), "expected_calls": [{
            "call_id": receipt.call_id, "job_id": receipt.job_id, "attempt_id": receipt.attempt_id,
            "stage_name": receipt.stage_name, "node_id": receipt.node_id, "closure_epoch_id": receipt.closure_epoch_id}],
        "observed_receipt_ids": [receipt.receipt_id]}}
    closure = _register(registry, workspace, "review:provider_receipt_closure", "provider_receipt_closure", closure_payload,
                        [ArtifactDependencyRefV2.from_record(ledger_record)])
    _revision, records = registry._read_registry_unlocked()
    entries = {stage: {"complete": True, "artifact_id": closure.artifact_id} for stage in ("outline", "review", "validate")}
    return workspace, registry, receipt, capsule, records, entries


def test_selected_registered_capture_is_resolved_read_only_from_real_ledger(tmp_path):
    workspace, registry, _receipt, capsule, records, entries = _captured_graph(tmp_path, capture=True)
    before = {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()}
    captures, closures = admission._capture_refs(workspace, registry, records, entries)
    assert captures == [capsule]
    assert len(closures) == 1
    assert {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()} == before


def test_selected_receipt_id_and_exact_dto_hash_must_exist_in_original_ledger(tmp_path):
    _workspace, _registry, receipt, _capsule, records, _entries = _captured_graph(tmp_path, capture=True)
    ledgers = [record for record in records.values() if record.artifact_type == "provider_receipt_ledger"]
    assert admission._receipt_by_id(ledgers, receipt.receipt_id, hash_json(receipt.to_dict())) == receipt
    with pytest.raises(SourceRevisionError, match="row hash differs"):
        admission._receipt_by_id(ledgers, receipt.receipt_id, "f" * 64)
    with pytest.raises(SourceRevisionError, match="missing or ambiguous"):
        admission._receipt_by_id(ledgers, "unknown-original-receipt")


def test_production_writer_validator_graphs_without_receipt_id_array_resolve_real_original_capture(tmp_path):
    workspace, registry, _receipt, capsule, _records, entries = _captured_graph(tmp_path, capture=True)
    closure = registry.get("review:provider_receipt_closure")
    document = json.loads(Path(closure.path).read_bytes())
    del document["payload"]["observed_receipt_ids"]
    _register(registry, workspace, closure.artifact_id, closure.artifact_type, document, closure.depends_on)
    _revision, records = registry._read_registry_unlocked()
    before = {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()}
    captures, _closures = admission._capture_refs(workspace, registry, records, entries)
    assert captures == [capsule]
    assert {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("mutation", ["missing_call_graph", "missing_receipt", "wrong_epoch"])
def test_receipt_id_free_graph_still_needs_exact_declared_owner_call_and_epoch(tmp_path, mutation):
    _workspace, _registry, receipt, _capsule, records, _entries = _captured_graph(tmp_path, capture=True)
    calls = [{"call_id": receipt.call_id, "job_id": receipt.job_id, "attempt_id": receipt.attempt_id,
              "stage_name": receipt.stage_name, "node_id": receipt.node_id, "closure_epoch_id": receipt.closure_epoch_id}]
    observed = [receipt.call_id]
    ledgers = [record for record in records.values() if record.artifact_type == "provider_receipt_ledger"]
    if mutation == "missing_call_graph": observed = None
    elif mutation == "missing_receipt": calls[0]["call_id"] = observed[0] = "missing-current-call"
    else: calls[0]["closure_epoch_id"] = "foreign-current-epoch"
    with pytest.raises(SourceRevisionError): admission._selected_call_receipts(ledgers, calls, observed)


def test_newest_unknown_outcome_never_falls_back_to_older_known_success(tmp_path):
    workspace, registry, receipt, _capsule, records, _entries = _captured_graph(tmp_path, capture=True)
    ledger = next(record for record in records.values() if record.artifact_type == "provider_receipt_ledger")
    unknown = replace(receipt, receipt_id="newer-unknown-outcome", sequence=receipt.sequence + 1,
                      finished_at="2099-01-01T00:00:00+00:00", status="failed", error_kind="outcome_unknown")
    Path(ledger.path).write_text(json.dumps(receipt.to_dict()) + "\n" + json.dumps(unknown.to_dict()) + "\n", encoding="utf-8")
    fresh_ledger = registry.register_file(artifact_id=ledger.artifact_id, artifact_role=ledger.artifact_role,
        artifact_type=ledger.artifact_type, artifact_version=ledger.artifact_version, path=ledger.path,
        producer="tests.source_revision_admission")
    calls = [{"call_id": receipt.call_id, "job_id": workspace.job_id, "attempt_id": receipt.attempt_id,
              "stage_name": receipt.stage_name, "node_id": receipt.node_id, "closure_epoch_id": receipt.closure_epoch_id}]
    with pytest.raises(SourceRevisionError, match="known success"):
        admission._selected_call_receipts([fresh_ledger], calls, [receipt.call_id])


def _single_variant_batch(tmp_path):
    selection = SummarySelectionSpecV1(
        parent_job_id="old-parent", parent_registry_path=str(tmp_path / "batch-parent" / "artifact_registry.json"),
        parent_artifact_id="summary_file", parent_content_hash="a" * 64,
        parent_summary_path=str(tmp_path / "batch-parent" / "summaries.json"),
        ordered_paper_keys=("paper-a",), expected_count=1,
    )
    return ReviewBatchSpecV1(project_name="revision", selection=selection).to_dict()


@pytest.mark.parametrize("entrypoint", ["build", "verify", "runner"])
@pytest.mark.parametrize("batch_value", ["explicit_none", "valid_single_variant"])
def test_first_mode_mixed_batch_rejects_before_config_parent_or_bootstrap(tmp_path, monkeypatch, entrypoint, batch_value):
    body, base_spec = _body(tmp_path)
    batch = None if batch_value == "explicit_none" else _single_variant_batch(tmp_path)
    spec = replace(base_spec, metadata={**base_spec.metadata, "review_batch_spec": batch})
    spec.validate()
    if batch is not None:
        parsed = admission.AgentRuntimeRunner._review_batch_spec(spec)
        assert parsed is not None and parsed.is_multi_variant is False
        assert not spec.summary_sources
        changed = replace(spec, summary_sources=(parsed.parent_selection().parent_summary_path,))
        assert child_core_spec_hash(spec.to_dict()) != child_core_spec_hash(changed.to_dict())
    body["child_core_spec_hash"] = child_core_spec_hash(spec.to_dict())
    bound = _bind(tmp_path, spec, body)

    def forbidden(*args, **kwargs):
        pytest.fail("mixed first-mode task reached configuration, parent authority, namespace or bootstrap")

    monkeypatch.setattr(admission, "_config", forbidden)
    monkeypatch.setattr(admission, "_child_namespace", forbidden)
    monkeypatch.setattr(admission.AgentRuntimeRunner, "status", forbidden)
    monkeypatch.setattr("runtime.runner.AgentRuntimeBridge", forbidden)
    if entrypoint == "runner":
        # Isolate the actual post-normalization Runner boundary. The real plan
        # ref/body verifier and shared admission implementation still execute.
        Path(spec.config).write_text("unchanged config bytes", encoding="utf-8")
        monkeypatch.setattr(admission.AgentRuntimeRunner, "_normalized_spec", lambda self, *, resume: self.job_spec)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(SourceRevisionError, match="does not support mixed review_batch_spec"):
        if entrypoint == "build":
            admission.build_source_revision_plan_v1(spec, Path(body["parent"]["workspace_path"]))
        elif entrypoint == "verify":
            admission.verify_source_revision_plan_v1(bound)
        else:
            admission.AgentRuntimeRunner(bound)._execute(resume=False)
    assert not Path(spec.workspace_path).exists()
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("mutation", ["missing", "tampered", "unregistered_private"])
def test_missing_or_bad_selected_capture_blocks_without_parent_repair(tmp_path, mutation):
    workspace, registry, receipt, capsule, records, entries = _captured_graph(tmp_path, capture=mutation == "tampered")
    if mutation == "tampered": Path(capsule.path).write_text("{}", encoding="utf-8")
    if mutation == "unregistered_private":
        store = ProviderResponseSnapshotStore(workspace, registry, LocalPublicationContext())
        # Even a genuine same-job staged capsule cannot be recovered by a child
        # admission read; only the parent capture path may publish it.
        original = registry.register_file
        try:
            registry.register_file = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("publication interrupted"))
            with pytest.raises(ProviderResponseSnapshotError): store.capture(receipt, {"source": "known original"})
        finally:
            registry.register_file = original
    before = {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()}
    with pytest.raises((ProviderResponseSnapshotError, UnverifiedArtifact)):
        admission._capture_refs(workspace, registry, records, entries)
    assert {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()} == before

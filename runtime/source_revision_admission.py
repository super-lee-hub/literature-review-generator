"""Read-only admission for one-PDF revisions of completed successor runs.

Parsing a plan or finding a capsule is not authority. Both public operations
reopen the parent Registry, its completed closure and the actual source bytes.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from config_loader import load_config
from runtime.job_spec import RuntimeJobSpec
from runtime.provider_receipt_closure import ExpectedProviderCall
from runtime.provider_response_snapshot import ProviderResponseSnapshotStore
from runtime.provider_runtime import ProviderAggregateBudgetV2, ProviderCallReceiptV1, hash_json
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.source_intake import build_direct_source_bundle, build_zotero_source_bundle, validate_f1_corpus_source_bundle
from runtime.source_revision import (
    PROVIDER_SEMANTIC_CONTRACT_V2,
    SourceRevisionError,
    SourceRevisionPlanRefV1,
    child_core_spec_hash,
    classify_replacement_delta,
    provider_semantic_contract_version,
)
from runtime.stage_contracts import SourceBundle
from runtime.stage_terminal import TerminalStageRecordV1
from services.artifact_registry import ArtifactRecord, ArtifactRegistry, RegistryError, file_sha256
from services.job_workspace import JobWorkspace
from services.paper_identity import build_canonical_paper_key
from services.queue_service import LocalPublicationContext
from services.source_identity import inspect_pdf_identity
from validation.closure import resolve_current_stage_closure_map

PLAN_VERSION = "source-revision-plan/v1"
PLAN_MODE = "replace-existing-pdf/v1"
_PLAN_FIELDS = frozenset({
    "schema_version", "mode", "provider_semantic_contract_version", "child_core_spec_hash",
    "child_job_id", "child_project_name", "child_workspace_path", "parent", "old_sources",
    "new_sources", "changed_paper_key", "unchanged_paper_keys", "review_intent_hash",
    "config_binding", "provider_budget",
})
_PARENT_FIELDS = frozenset({"workspace_path", "job_id", "registry_path", "authority_refs", "response_snapshot_refs"})
_REF_FIELDS = frozenset({"job_id", "artifact_id", "artifact_type", "artifact_version", "artifact_role", "path", "content_hash"})
_SOURCE_FIELDS = frozenset({"canonical_paper_key", "source_pdf", "pdf_sha256", "identity_hash"})


def _require(ok: Any, reason: str) -> None:
    if not ok:
        raise SourceRevisionError(reason)


def _read(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            _require(key not in result, "source revision JSON has duplicate keys")
            result[key] = value
        return result
    try:
        def invalid_constant(value: str) -> Any:
            raise SourceRevisionError(f"non-JSON numeric constant: {value}")
        return json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SourceRevisionError(f"source revision data cannot be read: {path.name}") from exc


def _absolute(path: str | Path, label: str) -> Path:
    _require(isinstance(path, (str, Path)) and str(path).strip(), f"{label} must be an explicit path")
    value = Path(path)
    _require(value.is_absolute() and ".." not in value.parts, f"{label} must be absolute and normalized")
    return value.resolve()


def _ref(record: ArtifactRecord) -> dict[str, str]:
    return {name: str(getattr(record, name)) for name in _REF_FIELDS}


def _ready(records: Mapping[str, ArtifactRecord], name: str, kind: str) -> ArtifactRecord:
    record = records.get(name)
    _require(record is not None and record.status == "ready" and record.artifact_type == kind,
             f"parent required READY authority is missing: {name}")
    assert record is not None
    return record


def _outline_terminal_authority(
    registry: ArtifactRegistry, records: Mapping[str, ArtifactRecord],
) -> ArtifactRecord:
    """Select the completed terminal that binds the current Outline health.

    Health depends on its accepted request audit. The provider closure alone
    cannot reach that dependent audit, so the terminal must be an explicit
    parent root. Unrelated stage records are filtered before any file read.
    """
    health = _ready(records, "outline-v3:stage_health", "outline_stage_health")
    matches: list[tuple[str, str, ArtifactRecord]] = []
    for registered in records.values():
        if (
            registered.status != "ready" or registered.artifact_type != "runtime_stage_terminal"
            or registered.artifact_version != "v1"
            or registered.metadata.get("stage_name") != "outline"
            or registered.metadata.get("stage_status") != "succeeded"
        ):
            continue
        _require(registered.job_id == registry.job_id, "foreign parent Outline terminal")
        try:
            ArtifactRegistry._verify_ready_artifact(registered)
            terminal = TerminalStageRecordV1.from_dict(_read(Path(registered.path)))
        except (RegistryError, ValueError, TypeError, OSError) as exc:
            raise SourceRevisionError("parent Outline terminal is invalid") from exc
        _require(
            terminal.job_id == registry.job_id and terminal.record_id == registered.artifact_id
            and terminal.stage_name == "outline" and terminal.status == "succeeded",
            "parent Outline terminal ownership/status differs",
        )
        if any(
            ref.dependency_kind == "local_job" and ref.job_id == registry.job_id
            and ref.artifact_id == health.artifact_id and ref.artifact_type == health.artifact_type
            and ref.content_hash == health.content_hash and Path(ref.path).resolve() == Path(health.path).resolve()
            for ref in terminal.output_artifact_refs
        ):
            matches.append((terminal.finished_at, registered.artifact_id, registered))
    _require(matches, "parent completed Outline terminal authority is missing")
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _config(spec: RuntimeJobSpec) -> tuple[Path, dict[str, Any]]:
    from runtime.test_dependencies import current_runtime_test_dependencies

    path = _absolute(spec.config, "configuration")
    _require(path.is_file(), "explicit configuration file is required")
    deps = current_runtime_test_dependencies()
    loaded = load_config(
        str(path), action=spec.action, requested_stages=spec.metadata.get("requested_stages"),
        free_mode_enabled=bool(spec.free_mode_profile or spec.free_mode_idea or spec.metadata.get("free_mode_input")),
        allow_template_credentials=bool(deps and deps.allow_template_credentials),
    )
    return path, dict(loaded)


def _child_namespace(spec: RuntimeJobSpec, config: Mapping[str, Any], *, allow_existing: bool) -> Path:
    target = _absolute(spec.workspace_path, "explicit child workspace")
    output = config.get("Paths", {}).get("output_path")
    _require(isinstance(output, str) and output, "configuration has no output root")
    expected = JobWorkspace(str(_absolute(output, "output root")), spec.project_name, spec.job_id)
    _require(target == Path(expected.root_dir), "child workspace differs from production bootstrap layout")
    if target.exists():
        _require(allow_existing and target.is_dir(), "new source revision child workspace must be fresh")
        registry_path = target / "artifact_registry.json"
        _require(registry_path.is_file(), "existing child has no registered runtime spec")
        child_registry = ArtifactRegistry(registry_path, spec.job_id)
        _rev, records = child_registry._read_registry_unlocked()
        record = _ready(records, "runtime_job_spec", "runtime_job_spec")
        ArtifactRegistry._verify_ready_artifact(record)
        _require(_read(Path(record.path)) == spec.to_dict(), "existing child runtime spec/owner differs")
        _require(record.job_id == spec.job_id and file_sha256(record.path) == record.content_hash
                 and child_registry._read_registry_unlocked() == (_rev, records), "existing child changed during admission")
    return target


def _source_bundle(spec: RuntimeJobSpec) -> SourceBundle:
    if spec.source.mode == "direct":
        bundle = build_direct_source_bundle(project_name=spec.project_name, pdf_folder=spec.source.pdf_folder)
    else:
        bundle = build_zotero_source_bundle(project_name=spec.project_name,
                                           zotero_report=spec.source.zotero_report, library_path=spec.source.library_path)
    binding = spec.metadata.get("f1_corpus_binding")
    if isinstance(binding, Mapping):
        bundle = validate_f1_corpus_source_bundle(bundle, binding=binding)
    _require(not bundle.source_snapshot.get("quarantined_sources"), "revision source contains quarantined identities")
    return bundle


def _source_rows(bundle: SourceBundle, *, original: SourceBundle | None = None) -> list[dict[str, str]]:
    rows = []
    originals = {item.canonical_paper_key: item for item in original.paper_work_items} if original else {}
    for item in bundle.paper_work_items:
        path = _absolute(item.source_pdf, "source PDF")
        _require(path.is_file() and path.suffix.casefold() == ".pdf", "revision source PDF is missing")
        actual = file_sha256(path)
        declared = str(item.source_descriptor.get("source_pdf_fingerprint") or "")
        _require(declared == actual, "registered source PDF byte identity is stale")
        _require(item.canonical_paper_key == build_canonical_paper_key(item.paper_info), "source canonical identity differs")
        prior = originals.get(item.canonical_paper_key)
        expected = dict(prior.paper_info if prior else item.paper_info)
        # Direct-source keys may come from a filename. Establish the original
        # PDF's independently observed bibliographic identity, then require the
        # replacement to match it. Observed identity excludes text and paths.
        observed = inspect_pdf_identity(expected, prior.source_pdf if prior else str(path)).observed
        expected = {**expected, **{key: value for key, value in observed.items()
                                  if key in {"title", "authors", "year", "doi"} and value
                                  and (item.source_mode == "direct" or not expected.get(key))}}
        identity = inspect_pdf_identity(expected, str(path))
        _require(identity.canonical_ready and identity.candidate_hash == actual,
                 f"source PDF bibliographic identity is not established: {item.canonical_paper_key}")
        rows.append({"canonical_paper_key": item.canonical_paper_key, "source_pdf": str(path),
                     "pdf_sha256": actual, "identity_hash": hash_json(identity.observed)})
    _require(len(rows) == len({row["canonical_paper_key"] for row in rows}), "source keys are duplicated")
    return rows


def _closure_payload(record: ArtifactRecord) -> dict[str, Any]:
    value = _read(Path(record.path))
    _require(isinstance(value, dict), "parent closure is not an object")
    inner = value.get("payload", value)
    _require(isinstance(inner, Mapping), "parent closure payload is not an object")
    result = dict(inner)
    for key in ("expected_calls", "observed_receipt_ids"):
        if key not in result and key in value:
            result[key] = value[key]
    return result


def _reachable_ledgers(record: ArtifactRecord, records: Mapping[str, ArtifactRecord]) -> list[ArtifactRecord]:
    found: dict[str, ArtifactRecord] = {}
    pending = [record]
    seen = set()
    while pending:
        current = pending.pop()
        if current.artifact_id in seen:
            continue
        seen.add(current.artifact_id)
        if current.artifact_type == "provider_receipt_ledger":
            found[current.artifact_id] = current
        for dependency in current.depends_on:
            if dependency.dependency_kind == "local_job" and dependency.job_id == current.job_id:
                child = records.get(dependency.artifact_id)
                _require(child is not None, "parent declared dependency is missing")
                if child is not None:
                    pending.append(child)
    return list(found.values())


def _ledger_receipts(ledgers: list[ArtifactRecord]) -> list[ProviderCallReceiptV1]:
    receipts = []
    for ledger in ledgers:
        ArtifactRegistry._verify_ready_artifact(ledger)
        for line in Path(ledger.path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                receipts.append(ProviderCallReceiptV1.from_dict(json.loads(line)))
    return receipts


def _receipt_by_id(ledgers: list[ArtifactRecord], receipt_id: str, row_hash: str = "", *,
                   known_receipts: list[ProviderCallReceiptV1] | None = None) -> ProviderCallReceiptV1:
    matches: dict[str, ProviderCallReceiptV1] = {}
    for receipt in _ledger_receipts(ledgers) if known_receipts is None else known_receipts:
        if receipt.receipt_id != receipt_id:
            continue
        digest = hash_json(receipt.to_dict())
        _require(not row_hash or digest == row_hash, "parent selected receipt row hash differs")
        matches[digest] = receipt
    _require(len(matches) == 1, "parent original receipt is missing or ambiguous")
    return next(iter(matches.values()))


def _selected_call_receipts(ledgers: list[ArtifactRecord], calls: list[Any], observed_calls: Any, *,
                            known_receipts: list[ProviderCallReceiptV1] | None = None) -> list[ProviderCallReceiptV1]:
    """Resolve S3/S4's actual graph format, which does not persist receipt IDs.

    Their canonical closure verifier has already proved the complete graph.
    Match its exact call/epoch scope and newest-outcome ordering; never choose
    an older success instead of a later failure or unknown outcome.
    """
    _require(isinstance(observed_calls, list) and all(isinstance(value, str) for value in observed_calls),
             "parent observed call graph is absent")
    receipts: dict[str, ProviderCallReceiptV1] = {}
    for receipt in _ledger_receipts(ledgers) if known_receipts is None else known_receipts:
        receipts[hash_json(receipt.to_dict())] = receipt
    selected = []
    for row in calls:
        _require(isinstance(row, Mapping), "parent expected call is malformed")
        call = ExpectedProviderCall.from_mapping(row)
        if call.verified_reuse:
            continue
        _require(call.call_id in observed_calls, "parent expected call has no observed outcome")
        candidates = [receipt for receipt in receipts.values()
                      if receipt.call_id == call.call_id and receipt.job_id == call.job_id
                      and receipt.stage_name == call.stage_name
                      and (not call.closure_epoch_id or receipt.closure_epoch_id == call.closure_epoch_id)]
        _require(candidates, "parent original receipt is missing from declared ledger")
        current = max(candidates, key=lambda item: (str(item.finished_at or ""), int(item.sequence), int(item.attempts)))
        _require(current.status == "success", "parent selected outcome is not a known success")
        selected.append(current)
    return selected


def _capture_refs(workspace: JobWorkspace, registry: ArtifactRegistry, records: Mapping[str, ArtifactRecord],
                  entries: Mapping[str, Any]) -> tuple[list[ArtifactRecord], list[ArtifactRecord]]:
    capsules: dict[str, ArtifactRecord] = {}
    closures: dict[str, ArtifactRecord] = {}
    # Operation-local verified bytes only. The final Registry/file closure
    # fence still runs; no Registry revision becomes a persistent cache key.
    ledger_rows: dict[tuple[str, str, str], list[ProviderCallReceiptV1]] = {}
    def rows_for(ledgers: list[ArtifactRecord]) -> list[ProviderCallReceiptV1]:
        result = []
        for ledger in ledgers:
            key = (ledger.artifact_id, ledger.path, ledger.content_hash)
            if key not in ledger_rows:
                ledger_rows[key] = _ledger_receipts([ledger])
            result.extend(ledger_rows[key])
        return result
    store = ProviderResponseSnapshotStore(workspace, registry, LocalPublicationContext())
    for stage in ("outline", "review", "validate"):
        entry = entries.get(stage)
        _require(isinstance(entry, Mapping) and entry.get("complete") is True, f"parent {stage} closure is incomplete")
        assert isinstance(entry, Mapping)
        closure = _ready(records, str(entry.get("artifact_id") or ""), "provider_receipt_closure")
        closures[closure.artifact_id] = closure
        payload = _closure_payload(closure)
        calls = payload.get("expected_calls")
        _require(isinstance(calls, list), "parent expected call graph is absent")
        assert isinstance(calls, list)
        ledgers = _reachable_ledgers(closure, records)
        receipt_rows = rows_for(ledgers)
        if "observed_receipt_ids" in payload:
            observed = payload["observed_receipt_ids"]
            _require(isinstance(observed, list), "parent observed receipt identities are malformed")
            assert isinstance(observed, list)
            observed_receipts = [_receipt_by_id(ledgers, str(receipt_id), known_receipts=receipt_rows) for receipt_id in observed]
            # Known failed retries have no successful response to capture.
            selected = [receipt for receipt in observed_receipts if receipt.status == "success"]
        else:
            selected = _selected_call_receipts(ledgers, calls, payload.get("observed_call_ids"), known_receipts=receipt_rows)
        for call in calls:
            _require(isinstance(call, Mapping), "parent expected call is malformed")
            if not call.get("verified_reuse"):
                continue
            proof = records.get(str(call.get("reuse_evidence_artifact_id") or ""))
            _require(proof is not None and proof.status == "ready", "parent reuse authority is missing")
            assert proof is not None
            _require(proof.artifact_version == "v1", "nested derived-parent reuse is not yet supported")
            if proof.artifact_type == "review_replay_ledger":
                replay_rows = [json.loads(line) for line in Path(proof.path).read_text(encoding="utf-8").splitlines() if line.strip()]
                matched = [row for row in replay_rows if hash_json(row) == call.get("reuse_evidence_record_hash")]
                _require(len(matched) == 1, "parent Writer original replay row is ambiguous")
                snap = records.get(str(matched[0].get("receipt_snapshot_artifact_id") or ""))
                _require(snap is not None, "parent Writer receipt snapshot is missing")
                assert snap is not None
                source = _read(Path(snap.path))
                selected.append(ProviderCallReceiptV1.from_dict(source["receipt"]))
            else:
                data = _read(Path(proof.path))
                inner = data.get("payload") if isinstance(data.get("payload"), Mapping) else data
                source = inner.get("source_authority") if isinstance(inner.get("source_authority"), Mapping) else inner
                receipt_id = str(source.get("source_receipt_id") or "")
                row_hash = str(source.get("source_receipt_record_hash") or source.get("source_receipt_hash") or "")
                _require(receipt_id and row_hash, "parent original-owner receipt cannot be resolved")
                source_ledgers = _reachable_ledgers(proof, records)
                selected.append(_receipt_by_id(source_ledgers, receipt_id, row_hash, known_receipts=rows_for(source_ledgers)))
        for receipt in selected:
            _require(receipt.job_id == workspace.job_id and receipt.status == "success", "foreign or failed parent source receipt")
            capsule = store.read_registered(receipt)  # Never require()/repair a parent.
            capsules[capsule.artifact_id] = capsule
    return list(capsules.values()), list(closures.values())


def _budget(value: Any) -> ProviderAggregateBudgetV2:
    _require(isinstance(value, Mapping), "source revision needs an explicit finite child budget")
    try:
        budget = ProviderAggregateBudgetV2.from_mapping(value)
    except (ValueError, TypeError) as exc:
        raise SourceRevisionError("source revision budget is not strict V2") from exc
    _require(budget.to_dict() == value and all(type(value[name]) is int for name in (
        "max_provider_calls_total", "max_output_tokens_total", "max_retry_attempts_total"))
        and type(value["max_wall_seconds"]) in (int, float), "source revision budget is not strict V2")
    _require(budget.max_provider_calls_total > 0 and budget.max_output_tokens_total > 0
             and budget.max_wall_seconds > 0, "source revision needs explicit finite child authority")
    return budget


def _validate_body(body: Any) -> None:
    _require(isinstance(body, Mapping) and set(body) == _PLAN_FIELDS, "source revision plan fields are incomplete or unknown")
    _require(body["schema_version"] == PLAN_VERSION and body["mode"] == PLAN_MODE
             and body["provider_semantic_contract_version"] == PROVIDER_SEMANTIC_CONTRACT_V2, "source revision plan version/mode is invalid")
    def text(value: Any) -> None:
        _require(isinstance(value, str) and bool(value.strip()), "source revision text field is invalid")
    def digest(value: Any) -> None:
        _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
                 "source revision hash field is invalid")
    for name in ("child_job_id", "child_project_name", "changed_paper_key"):
        text(body[name])
    for name in ("child_core_spec_hash", "review_intent_hash"):
        digest(body[name])
    _absolute(body["child_workspace_path"], "child workspace")
    _require(isinstance(body["unchanged_paper_keys"], list), "unchanged paper keys must be an array")
    for key in body["unchanged_paper_keys"]:
        text(key)
    parent = body["parent"]
    _require(isinstance(parent, Mapping) and set(parent) == _PARENT_FIELDS, "source revision parent fields differ")
    text(parent["job_id"])
    _absolute(parent["workspace_path"], "parent workspace")
    _absolute(parent["registry_path"], "parent Registry")
    for name in ("authority_refs", "response_snapshot_refs"):
        _require(isinstance(parent[name], list), "source revision parent refs must be arrays")
        for ref in parent[name]:
            _require(isinstance(ref, Mapping) and set(ref) == _REF_FIELDS, "source revision authority ref fields differ")
            for field in _REF_FIELDS - {"content_hash", "path"}:
                text(ref[field])
            _require(ref["job_id"] == parent["job_id"], "foreign source revision authority")
            digest(ref["content_hash"])
            _absolute(ref["path"], "authority path")
        _require(len(parent[name]) == len({ref["artifact_id"] for ref in parent[name]}), "duplicate parent authority refs")
    for name in ("old_sources", "new_sources"):
        _require(isinstance(body[name], list) and body[name], "source revision source map is empty")
        for row in body[name]:
            _require(isinstance(row, Mapping) and set(row) == _SOURCE_FIELDS, "source revision PDF map fields differ")
            text(row["canonical_paper_key"])
            _absolute(row["source_pdf"], "source PDF")
            digest(row["pdf_sha256"])
            digest(row["identity_hash"])
    _require(isinstance(body["config_binding"], Mapping) and set(body["config_binding"]) == {
        "parent_path", "child_path", "file_sha256"}, "source revision configuration binding differs")
    for name in ("parent_path", "child_path"):
        _absolute(body["config_binding"][name], "configuration path")
    digest(body["config_binding"]["file_sha256"])
    _budget(body["provider_budget"])


def _build(spec: RuntimeJobSpec, parent_path: Path, *, allow_existing_child: bool) -> dict[str, Any]:
    spec.validate()
    _require(provider_semantic_contract_version(spec.metadata) == PROVIDER_SEMANTIC_CONTRACT_V2,
             "source revision requires explicit successor contract")
    _require(spec.job_id and spec.action == "run_all", "source revision requires explicit child job ID and run_all")
    _require("review_batch_spec" not in spec.metadata,
             "replace-existing-pdf/v1 does not support mixed review_batch_spec tasks")
    child_config_path, child_config = _config(spec)
    child_workspace = _child_namespace(spec, child_config, allow_existing=allow_existing_child)
    parent_path = _absolute(parent_path, "parent workspace")
    _require(parent_path.is_dir() and child_workspace != parent_path
             and parent_path not in child_workspace.parents and child_workspace not in parent_path.parents,
             "source revision parent/child workspaces must be disjoint")
    parent_workspace, registry = AgentRuntimeRunner._open_workspace(parent_path)
    _require(spec.job_id != parent_workspace.job_id, "source revision must use a new child job ID")
    # All real publication paths have a durable nonempty Registry lock. Refuse
    # rather than creating a missing lock while reading an alleged parent.
    _require(Path(registry.lock_path).is_file() and Path(registry.lock_path).stat().st_size > 0,
             "parent has no established Registry publication boundary")
    before_revision, records = registry._read_registry_unlocked()
    runtime_spec = _ready(records, "runtime_job_spec", "runtime_job_spec")
    parent_spec = RuntimeJobSpec.from_mapping(_read(Path(runtime_spec.path)))
    _require(provider_semantic_contract_version(parent_spec.metadata) == PROVIDER_SEMANTIC_CONTRACT_V2,
             "legacy parent cannot grant successor downstream reuse")
    _require(parent_spec.job_id == parent_workspace.job_id and spec.source.mode == parent_spec.source.mode,
             "parent source/spec ownership differs")
    try:
        status = AgentRuntimeRunner.status(parent_path)
    except (OSError, ValueError, RuntimeRunnerError) as exc:
        raise SourceRevisionError("parent is not a completed canonical full pipeline") from exc
    _require(status.canonical_ready and status.completion_status == "complete" and status.job_status == "completed"
             and not status.requires_attention and set(("analyze", "outline", "review", "validate")).issubset(status.completed_stages),
             "parent is not a completed canonical full pipeline")
    current = registry.resolve_current_artifact_set()
    _require(current is not None and current.validation_status == "clean", "parent current set is not clean")
    assert current is not None
    source_record = _ready(records, "source_bundle", "source_bundle")
    old_bundle = SourceBundle.from_dict(_read(Path(source_record.path)))
    parent_config_path, _parent_config = _config(parent_spec)
    config_sha = file_sha256(parent_config_path)
    _require(config_sha == file_sha256(child_config_path), "configuration bytes changed")
    from runtime.provider_run_envelope import ProviderRunEnvelopeV1
    from services.job_fingerprint import FingerprintInputs, build_fingerprint_bundle, sanitize_config_for_fingerprint

    envelope_record = _ready(records, "provider_run_envelope", "provider_run_envelope")
    envelope = ProviderRunEnvelopeV1.from_dict(_read(Path(envelope_record.path)))
    effective_config_hash = build_fingerprint_bundle(FingerprintInputs(
        config_snapshot=sanitize_config_for_fingerprint(_parent_config), source_snapshot={}, request_snapshot={},
    )).config_hash
    child_config_hash = build_fingerprint_bundle(FingerprintInputs(
        config_snapshot=sanitize_config_for_fingerprint(child_config), source_snapshot={}, request_snapshot={},
    )).config_hash
    _require(envelope.job_id == parent_workspace.job_id
             and envelope.scope_hashes["effective_config_sha256"] == effective_config_hash == child_config_hash,
             "parent configuration changed since its finite run admission")
    intent = spec.metadata.get("review_intent")
    _require(isinstance(intent, Mapping) and intent and intent == parent_spec.metadata.get("review_intent"),
             "source revision review intent changed or is not explicit")
    budget = _budget(spec.metadata.get("provider_budget"))
    old_rows = _source_rows(old_bundle)
    new_rows = _source_rows(_source_bundle(spec), original=old_bundle)
    delta = classify_replacement_delta(
        [{key: row[key] for key in ("canonical_paper_key", "source_pdf", "pdf_sha256")} for row in old_rows],
        [{key: row[key] for key in ("canonical_paper_key", "source_pdf", "pdf_sha256")} for row in new_rows],
    )
    _require([row["identity_hash"] for row in old_rows] == [row["identity_hash"] for row in new_rows],
             "PDF bibliographic identity changed")
    resolver = AgentRuntimeRunner._external_registry_resolver(parent_workspace,
        registry_paths=AgentRuntimeRunner._review_batch_registry_paths(registry))
    closure_map = resolve_current_stage_closure_map(registry, external_registry_resolver=resolver).to_dict()
    capsules, stage_closures = _capture_refs(parent_workspace, registry, records, closure_map["provider_closures_by_stage"])
    analyze = closure_map["provider_closures_by_stage"].get("analyze")
    _require(isinstance(analyze, Mapping) and analyze.get("complete") is True, "parent analyze closure is incomplete")
    analyze_closure = _ready(records, str(analyze.get("artifact_id") or ""), "provider_receipt_closure")
    required = [runtime_spec, source_record, _ready(records, "source_inventory", "source_inventory"),
                _ready(records, "job_outcome", "job_outcome"), _ready(records, current.set_id, "current_artifact_set"),
                envelope_record, analyze_closure, _outline_terminal_authority(registry, records),
                *stage_closures, *capsules]
    registry.verify_ready_artifact_closures(required, external_registry_resolver=resolver,
                                           expected_record_snapshot=(before_revision, records))
    body = {
        "schema_version": PLAN_VERSION, "mode": PLAN_MODE,
        "provider_semantic_contract_version": PROVIDER_SEMANTIC_CONTRACT_V2,
        "child_core_spec_hash": child_core_spec_hash(spec.to_dict()), "child_job_id": spec.job_id,
        "child_project_name": spec.project_name, "child_workspace_path": str(child_workspace),
        "parent": {"workspace_path": str(parent_path), "job_id": parent_workspace.job_id,
                   "registry_path": registry.registry_path, "authority_refs": [_ref(row) for row in required if row not in capsules],
                   "response_snapshot_refs": sorted((_ref(row) for row in capsules), key=lambda row: row["artifact_id"])},
        "old_sources": old_rows, "new_sources": new_rows, "changed_paper_key": delta.changed_paper_key,
        "unchanged_paper_keys": list(delta.unchanged_paper_keys), "review_intent_hash": hash_json(intent),
        "config_binding": {"parent_path": str(parent_config_path), "child_path": str(child_config_path), "file_sha256": config_sha},
        "provider_budget": budget.to_dict(),
    }
    _validate_body(body)
    _require(registry._read_registry_unlocked() == (before_revision, records), "parent Registry changed during revision admission")
    _require(old_rows == _source_rows(old_bundle) and new_rows == _source_rows(_source_bundle(spec), original=old_bundle)
             and file_sha256(parent_config_path) == file_sha256(child_config_path) == config_sha,
             "source/config bytes changed during revision admission")
    return body


def build_source_revision_plan_v1(normalized_child_spec: RuntimeJobSpec, parent_workspace: Path) -> dict[str, Any]:
    """Build a strict plan without saving files, creating a job, or dispatching."""
    return _build(normalized_child_spec, parent_workspace, allow_existing_child=False)


def verify_source_revision_plan_v1(normalized_child_spec: RuntimeJobSpec) -> dict[str, Any]:
    """Rebuild exact authority, allowing only an actual same-spec child resume."""
    reference = SourceRevisionPlanRefV1.from_mapping(normalized_child_spec.metadata.get("source_revision_plan", {}))
    path = _absolute(reference.path, "plan ref")
    _require(path.is_file() and file_sha256(path) == reference.file_sha256, "source revision plan file hash differs")
    body = _read(path)
    _validate_body(body)
    _require(hash_json(body) == reference.content_hash, "source revision plan content hash differs")
    expected = _build(normalized_child_spec, Path(body["parent"]["workspace_path"]), allow_existing_child=True)
    _require(body == expected, "source revision plan differs from actual parent/child/source authority")
    _require(file_sha256(path) == reference.file_sha256, "source revision plan changed during verification")
    return body

from __future__ import annotations

import json
import socket
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import services.stage1_analysis_service as stage1_module
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import atomic_write_json
from tests.test_current_stage1_generation import _service, _write_pdf


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("local predeclaration unit control attempted a connection")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def _case(
    tmp_path: Path, *, edges: bool = True, overrides: dict[str, Any] | None = None
):
    pdf = tmp_path / "paper-0.pdf"
    _write_pdf(pdf)
    policy = {
        "parser_mode": "hybrid",
        "primary_parser": "local",
        "fallback_parser": "local",
        "extractor_profile": "auto",
        "enable_local_rag": "false",
        "local_rag_allow_model_download": "false",
        **(overrides or {}),
    }
    service, bundle = _service(
        tmp_path,
        pdf,
        lambda **_kwargs: pytest.fail("Reader called"),
        config_overrides={"Preprocess": policy},
    )
    items = [bundle.paper_work_items[0]]
    for index in (1, 2):
        extra = tmp_path / f"paper-{index}.pdf"
        _write_pdf(extra)
        items.append(
            replace(
                items[0],
                canonical_paper_key=f"paper-{index}",
                source_paper_id=f"paper-{index}",
                source_pdf=str(extra),
            )
        )
        service.registry.register_file(
            artifact_id=f"source_pdf:{extra.name}",
            artifact_role="source_pdf",
            artifact_type="source_pdf",
            artifact_version="v1",
            path=extra,
            producer="unit",
        )
    bundle = replace(bundle, paper_work_items=items)
    service._ensure_durable_input_records(bundle)
    if edges:
        service.registry.update_record(
            "source_bundle",
            depends_on=[
                ArtifactDependencyRefV2.from_record(
                    service.registry.get(f"source_pdf:{Path(item.source_pdf).name}")
                )
                for item in items
            ],
        )
    service._freeze_source_scope(bundle)
    return service, bundle


def _prepared(service: Any, item: Any) -> Any:
    return SimpleNamespace(
        item=item,
        previous={"test_reuse": True},
        current_binding={"paper": item.canonical_paper_key},
        reuse_eligibility=None,
        primary_config=dict(service.settings.section("Primary_Reader_API")),
        backup_config=dict(service.settings.section("Backup_Reader_API")),
        stage1_input_settings=dict(service.settings.section("Stage1_Input")),
        predeclared_preprocess_authority=None,
        mineru_receipt_record=None,
        built_input=SimpleNamespace(
            visual_coverage={},
            visual_scan_candidate_refs=[],
            visual_scan_batches=[],
            user_message_content=[{"type": "text", "text": item.canonical_paper_key}],
            prompt_text=f"unit paper {item.canonical_paper_key}",
            prompt_id=service._stage1_user_prompt_identity.prompt_id,
            prompt_version=service._stage1_user_prompt_identity.version,
            prompt_sha256=service._stage1_user_prompt_identity.sha256,
        ),
    )


def _consume(service: Any, bundle: Any):
    generator = getattr(service, "_prepare_predeclared_items", None)
    prepared = (
        generator(bundle, {}, _verification_scope=None)
        if generator
        else (
            service._prepare_item(item, None, _verification_scope=None)
            for item in bundle.paper_work_items
        )
    )
    return service._predeclare_expected_calls(bundle, prepared)


def _install_parser(
    service: Any, monkeypatch: pytest.MonkeyPatch, after=None, *, cold=False
):
    parsed = []
    monkeypatch.setattr(
        service, "_stage1_binding_identity_hash", lambda _binding: "unit-binding"
    )

    def prepare(
        item, previous, *, _verification_scope=None, _predeclared_source_scope=None
    ):
        if _predeclared_source_scope is None:
            service._verify_source_scope_item(item)
        else:
            service._verify_source_scope_item(
                item, _predeclared_source_scope=_predeclared_source_scope
            )
        parsed.append(item.canonical_paper_key)
        if after:
            after(item, len(parsed))
        result = _prepared(service, item)
        if cold:
            result.previous = None
        return result

    monkeypatch.setattr(service, "_prepare_item", prepare)
    return parsed


def _count_full_scope(service: Any, monkeypatch: pytest.MonkeyPatch):
    calls = []
    verify = service.registry.verify_ready_artifact_closure

    def counted(record, *args, **kwargs):
        if getattr(record, "artifact_id", "") == "stage1_source_scope":
            calls.append(record.artifact_id)
        return verify(record, *args, **kwargs)

    monkeypatch.setattr(service.registry, "verify_ready_artifact_closure", counted)
    return calls


@pytest.mark.parametrize(
    "route",
    [
        "local",
        "fitz",
        "remote",
        "unknown",
        "rag",
        "download",
        "missing_flag",
        "invalid_flag",
        "missing_parser",
    ],
)
def test_only_proven_local_predeclaration_batches_full_source_proofs(
    tmp_path, monkeypatch, route
):
    overrides = {"primary_parser": "mineru_remote"} if route == "remote" else {}
    service, bundle = _case(tmp_path, overrides=overrides)
    if route == "fitz":
        service.config["Preprocess"]["extractor_profile"] = "fitz"
    if route == "unknown":
        service.config["Preprocess"]["extractor_profile"] = "unknown-future-parser"
    if route in {"rag", "download"}:
        service.config["Preprocess"][
            "enable_local_rag" if route == "rag" else "local_rag_allow_model_download"
        ] = "true"
    if route == "missing_flag":
        service.config["Preprocess"].pop("enable_local_rag")
    if route == "invalid_flag":
        service.config["Preprocess"]["enable_local_rag"] = "unknown"
    if route == "missing_parser":
        service.config["Preprocess"].pop("primary_parser")
    parsed = _install_parser(service, monkeypatch)
    calls = _count_full_scope(service, monkeypatch)
    declarations = _consume(service, bundle)
    assert parsed == [item.canonical_paper_key for item in bundle.paper_work_items]
    assert len(declarations) == 3
    assert service.registry.get("stage1:provider_expected_call_graph") is not None
    assert len(calls) == (2 if route in {"local", "fitz"} else 3)


def _rewrite(registry, artifact_id, **changes):
    raw = json.loads(Path(registry.registry_path).read_text(encoding="utf-8"))
    row = next(row for row in raw["artifacts"] if row["artifact_id"] == artifact_id)
    row.update(changes)
    atomic_write_json(registry.registry_path, raw)


@pytest.mark.parametrize(
    "change",
    [
        "scope_metadata_false_zero",
        "bundle_edge",
        "leaf_metadata_false_zero",
        "late_pdf_bytes",
        "no_edges_pdf_bytes",
        "scope_bytes",
        "scope_path",
        "removed_leaf",
        "unrelated_append",
    ],
)
def test_tail_fences_exact_participating_records_and_all_frozen_pdf_bytes(
    tmp_path, monkeypatch, change
):
    service, bundle = _case(tmp_path, edges=change != "no_edges_pdf_bytes")
    source_id = f"source_pdf:{Path(bundle.paper_work_items[0].source_pdf).name}"
    scope = service.registry.get("stage1_source_scope")

    def after(_item, count):
        if count != len(bundle.paper_work_items):
            return
        if change == "scope_metadata_false_zero":
            _rewrite(service.registry, "stage1_source_scope", metadata={"numeric": 0})
        elif change == "bundle_edge":
            _rewrite(service.registry, "source_bundle", depends_on=[])
        elif change == "leaf_metadata_false_zero":
            _rewrite(service.registry, source_id, metadata={"numeric": 0})
        elif change in {"late_pdf_bytes", "no_edges_pdf_bytes"}:
            path = Path(bundle.paper_work_items[0].source_pdf)
            path.write_bytes(path.read_bytes() + b" ")
        elif change == "scope_bytes":
            path = Path(scope.path)
            path.write_bytes(path.read_bytes() + b" ")
        elif change == "scope_path":
            alternative = tmp_path / "alternative-scope.json"
            alternative.write_bytes(Path(scope.path).read_bytes())
            _rewrite(service.registry, "stage1_source_scope", path=str(alternative))
        elif change == "removed_leaf":
            raw = json.loads(Path(service.registry.registry_path).read_text())
            raw["artifacts"] = [
                row for row in raw["artifacts"] if row["artifact_id"] != source_id
            ]
            atomic_write_json(service.registry.registry_path, raw)
        else:
            independent = ArtifactRegistry(
                service.registry.registry_path, service.job_id
            )
            path = tmp_path / "diagnostic.json"
            path.write_text("{}")
            independent.register_file(
                artifact_id="unrelated",
                artifact_role="unit",
                artifact_type="unit",
                artifact_version="v1",
                path=path,
                producer="unit",
            )

    if change == "scope_metadata_false_zero":
        service.registry.update_record(
            "stage1_source_scope", metadata_updates={"numeric": False}
        )
        service._source_scope_record = service.registry.get("stage1_source_scope")
    elif change == "leaf_metadata_false_zero":
        service.registry.update_record(source_id, metadata_updates={"numeric": False})
    parsed = _install_parser(service, monkeypatch, after)
    if change == "unrelated_append":
        assert len(_consume(service, bundle)) == 3
        assert service.registry.get("stage1:provider_expected_call_graph") is not None
    else:
        with pytest.raises((RuntimeError, ValueError, OSError)):
            _consume(service, bundle)
        assert service.registry.get("stage1:provider_expected_call_graph") is None
    assert len(parsed) == 3


def test_default_prepare_and_jit_still_require_whole_source_scope(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)
    calls = _count_full_scope(service, monkeypatch)

    @contextmanager
    def stop_parser(*args, **kwargs):
        raise RuntimeError("parser boundary reached")
        yield  # pragma: no cover

    monkeypatch.setattr(service, "_preprocess", stop_parser)
    with pytest.raises(RuntimeError, match="parser boundary"):
        service._prepare_item(bundle.paper_work_items[0], None)
    assert len(calls) == 1

    def stop_jit(**kwargs):
        raise RuntimeError("JIT boundary reached")

    monkeypatch.setattr(service, "_hydrate_predeclared_preprocess_authority", stop_jit)
    with pytest.raises(RuntimeError, match="JIT boundary"):
        service._prepare_item(
            bundle.paper_work_items[0],
            None,
            predeclared_preprocess_authority=SimpleNamespace(),
        )
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["scope_record", "own_pdf", "remote_route"])
def test_current_item_rejects_late_source_or_route_change_before_its_parser(
    tmp_path, monkeypatch, change
):
    service, bundle = _case(tmp_path)
    if change == "scope_record":
        service.registry.update_record(
            "stage1_source_scope", metadata_updates={"numeric": False}
        )
        service._source_scope_record = service.registry.get("stage1_source_scope")

    def after(_item, count):
        if count != 1:
            return
        if change == "scope_record":
            _rewrite(service.registry, "stage1_source_scope", metadata={"numeric": 0})
        elif change == "own_pdf":
            path = Path(bundle.paper_work_items[1].source_pdf)
            path.write_bytes(path.read_bytes() + b" ")
        else:
            service.config["Preprocess"]["primary_parser"] = "mineru_remote"

    parsed = _install_parser(service, monkeypatch, after)
    with pytest.raises(RuntimeError, match="(source|binding|route)"):
        _consume(service, bundle)
    assert len(parsed) == 1
    assert service.registry.get("stage1:provider_expected_call_graph") is None
    assert stage1_module._PREDECLARATION_SOURCE_SCOPE.get() is None


def test_batched_predeclaration_keeps_graph_bytes_and_logical_identity(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)
    _install_parser(service, monkeypatch, cold=True)
    old_items = (
        service._prepare_item(item, None, _verification_scope=None)
        for item in bundle.paper_work_items
    )
    service._predeclare_expected_calls(bundle, old_items)
    assert len(service.expected_calls) == len(bundle.paper_work_items)
    before = Path(
        service.registry.get("stage1:provider_expected_call_graph").path
    ).read_bytes()
    identity = (service.expected_call_graph_hash, service.closure_epoch_id)
    _consume(service, bundle)
    after = Path(
        service.registry.get("stage1:provider_expected_call_graph").path
    ).read_bytes()
    assert before == after
    assert identity == (service.expected_call_graph_hash, service.closure_epoch_id)


@pytest.mark.parametrize("boundary", ["entry", "tail", "graph"])
def test_cancellation_has_no_parser_or_graph_after_its_boundary(
    tmp_path, monkeypatch, boundary
):
    service, bundle = _case(tmp_path)
    cancelled = [boundary == "entry"]

    def check():
        if cancelled[0]:
            raise RuntimeError("unit cancellation")

    service.cancellation_checker = check
    if boundary == "entry":
        parsers = []
        monkeypatch.setattr(
            service, "_preprocess", lambda *args, **kwargs: parsers.append(True)
        )
        with pytest.raises(RuntimeError, match="unit cancellation"):
            service._prepare_item(bundle.paper_work_items[0], None)
        assert parsers == []
    else:

        def after(_item, count):
            if boundary == "tail" and count == len(bundle.paper_work_items):
                cancelled[0] = True

        _install_parser(service, monkeypatch, after)
        if boundary == "graph":
            append = service._append_mineru_provider_receipts

            def cancel_before_graph():
                append()
                cancelled[0] = True

            monkeypatch.setattr(
                service, "_append_mineru_provider_receipts", cancel_before_graph
            )
        with pytest.raises(RuntimeError, match="unit cancellation"):
            _consume(service, bundle)
    assert service.registry.get("stage1:provider_expected_call_graph") is None


def test_prepare_exception_does_not_reuse_a_success_flag_or_skip_a_later_tail(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)

    def fail(_item, count):
        if count == 2:
            raise RuntimeError("unit parser failure")

    _install_parser(service, monkeypatch, fail)
    with pytest.raises(RuntimeError, match="unit parser failure"):
        _consume(service, bundle)
    assert service.registry.get("stage1:provider_expected_call_graph") is None
    assert not any("predeclare_scope" in name for name in vars(service))
    _install_parser(service, monkeypatch)
    calls = _count_full_scope(service, monkeypatch)
    assert len(_consume(service, bundle)) == 3
    assert len(calls) == 2


def test_run_closes_predeclaration_generator_when_its_consumer_raises(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)
    _install_parser(service, monkeypatch)
    observed = []

    def failing_consumer(_bundle, prepared):
        next(prepared)
        observed.append(prepared)
        raise RuntimeError("unit consumer failure")

    monkeypatch.setattr(service, "_predeclare_expected_calls", failing_consumer)
    with pytest.raises(RuntimeError, match="unit consumer failure"):
        service.run(bundle)
    assert len(observed) == 1
    assert observed[0].gi_frame is None
    assert service.registry.get("stage1:provider_expected_call_graph") is None
    assert stage1_module._PREDECLARATION_SOURCE_SCOPE.get() is None


def test_forged_data_tuple_cannot_admit_changed_pdf_to_direct_prepare(
    tmp_path, monkeypatch
):
    import hashlib

    service, bundle = _case(tmp_path)
    item = bundle.paper_work_items[0]
    record = service.registry.get("stage1_source_scope")
    assert record is not None
    path = Path(item.source_pdf)
    path.write_bytes(path.read_bytes() + b" ")
    row = dict(service._source_scope_by_paper[item.canonical_paper_key])
    row["source_pdf_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    forged = (
        record,
        json.dumps(
            asdict(record),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
        {item.canonical_paper_key: row},
    )
    parsers = []
    monkeypatch.setattr(
        service, "_preprocess", lambda *args, **kwargs: parsers.append(True)
    )
    with pytest.raises(RuntimeError, match="token is not active"):
        service._prepare_item(item, None, _predeclared_source_scope=forged)
    assert parsers == []


def test_active_token_is_owner_bound_and_frozen_and_default_still_full_checks(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign, foreign_bundle = _case(foreign_root)
    observed = []
    calls = _count_full_scope(service, monkeypatch)

    def after(item, count):
        if count != 1:
            return
        active = stage1_module._PREDECLARATION_SOURCE_SCOPE.get()
        assert active is not None and active[0] is service
        token: Any = active[1]
        observed.append(token)
        with pytest.raises(TypeError):
            token[2][item.canonical_paper_key] = {}
        with pytest.raises(TypeError):
            token[2][item.canonical_paper_key]["source_pdf_sha256"] = "changed"
        with pytest.raises(RuntimeError, match="token is not active"):
            foreign._verify_source_scope_item(
                foreign_bundle.paper_work_items[0], _predeclared_source_scope=token
            )
        service._verify_source_scope_item(
            item
        )  # Default None keeps its full proof even inside context.

    _install_parser(service, monkeypatch, after)
    _consume(service, bundle)
    assert len(observed) == 1
    assert len(calls) == 3  # Initial + explicit default check + tail.
    assert stage1_module._PREDECLARATION_SOURCE_SCOPE.get() is None


def test_closed_generator_revokes_token_in_original_and_copied_contexts(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)
    captured: list[tuple[Any, Any]] = []

    def after(_item, count):
        if count == 1:
            captured.append(
                (stage1_module._PREDECLARATION_SOURCE_SCOPE.get()[1], copy_context())
            )

    _install_parser(service, monkeypatch, after)
    generator = service._prepare_predeclared_items(bundle, {}, _verification_scope=None)
    next(generator)
    token, copied = captured[0]
    generator.close()
    assert generator.gi_frame is None
    assert stage1_module._PREDECLARATION_SOURCE_SCOPE.get() is None
    for context in (None, copied):

        def reuse():
            service._verify_source_scope_item(
                bundle.paper_work_items[0], _predeclared_source_scope=token
            )

        with pytest.raises(RuntimeError, match="token is not active"):
            reuse() if context is None else context.run(reuse)


def test_reentrant_predeclaration_rejects_without_disrupting_outer_cleanup(
    tmp_path, monkeypatch
):
    service, bundle = _case(tmp_path)

    def after(item, count):
        if count == 1:
            active = stage1_module._PREDECLARATION_SOURCE_SCOPE.get()
            assert active is not None
            token: Any = active[1]
            nested = service._prepare_predeclared_items(
                bundle, {}, _verification_scope=None
            )
            with pytest.raises(RuntimeError, match="already active"):
                next(nested)
            assert nested.gi_frame is None
            assert stage1_module._PREDECLARATION_SOURCE_SCOPE.get() is active
            service._verify_source_scope_item(item, _predeclared_source_scope=token)

    _install_parser(service, monkeypatch, after)
    _consume(service, bundle)
    assert stage1_module._PREDECLARATION_SOURCE_SCOPE.get() is None

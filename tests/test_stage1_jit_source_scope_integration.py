"""JIT generator boundaries with real source proofs and controlled provider spies."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.artifact_registry import RegistryError
from tests.test_stage1_local_predeclare_scope import _case


@dataclass
class _Prepared:
    item: Any
    current_binding: dict
    previous: Any = None
    reuse_eligibility: Any = None


def _generator_case(tmp_path, monkeypatch):
    service, bundle = _case(tmp_path)
    proofs, prepared_keys = [], []

    def prepare(item, _previous, **kwargs):
        proof = kwargs["_jit_source_scope_verifier"]
        assert proof is not None and proof._guard_thread is not None
        service._verify_source_scope_item(item, _jit_source_scope_verifier=proof)
        proofs.append(proof)
        prepared_keys.append(item.canonical_paper_key)
        return _Prepared(item, {"paper": item.canonical_paper_key})

    monkeypatch.setattr(service, "_prepare_item", prepare)
    monkeypatch.setattr(service, "_assert_jit_declaration_matches", lambda *_args: None)
    monkeypatch.setattr(service, "_bind_execution_provenance", lambda value: value)
    declarations = tuple(SimpleNamespace(
        item=item, predeclared_preprocess_authority=None, previous=None, reuse_eligibility=None,
    ) for item in bundle.paper_work_items)
    return service, bundle, declarations, proofs, prepared_keys


def test_source_change_after_declaration_assertion_blocks_before_provider(tmp_path, monkeypatch):
    service, bundle, declarations, proofs, prepared = _generator_case(tmp_path, monkeypatch)
    calls = []

    def after_assertion(*_args):
        path = Path(bundle.paper_work_items[-1].source_pdf)
        path.write_bytes(path.read_bytes() + b"late source mutation")

    monkeypatch.setattr(service, "_assert_jit_declaration_matches", after_assertion)
    monkeypatch.setattr(service, "_execute_prepared", lambda *_args, **_kwargs: calls.append("sent"))
    with pytest.raises(RegistryError, match="source bytes changed"):
        with closing(service._prepare_jit_items(declarations, None)) as items:
            for item in items:
                service._execute_prepared(item)
    assert len(prepared) == 1 and calls == []
    assert proofs and proofs[0]._active is False


def test_provider_failure_closes_proof_without_preparing_next_item(tmp_path, monkeypatch):
    service, _bundle, declarations, proofs, prepared = _generator_case(tmp_path, monkeypatch)

    def provider_failure(_prepared):
        assert proofs[0]._guard_thread is None
        raise OSError("original provider failure")

    monkeypatch.setattr(service, "_execute_prepared", provider_failure)
    with pytest.raises(OSError, match="original provider failure"):
        with closing(service._prepare_jit_items(declarations, None)) as items:
            for item in items:
                service._execute_prepared(item)
    assert len(prepared) == 1
    assert proofs[0]._active is False
    with pytest.raises(RuntimeError, match="not active"):
        proofs[0].verify(service._source_scope_record)


def test_all_provider_spies_run_after_guard_exit_and_can_append_outputs(tmp_path, monkeypatch):
    service, bundle, declarations, proofs, prepared = _generator_case(tmp_path, monkeypatch)
    calls = []

    def execute(item):
        assert proofs[-1]._active is True and proofs[-1]._guard_thread is None
        calls.append(item.item.canonical_paper_key)
        path = tmp_path / f"generated-{len(calls)}.txt"
        path.write_text("ordinary generated output", encoding="utf-8")
        service.registry.register_file(
            artifact_id=f"generated-output:{len(calls)}", artifact_role="generated_output",
            artifact_type="text_artifact", artifact_version="v1", path=path,
            producer="tests.jit-boundary",
        )

    monkeypatch.setattr(service, "_execute_prepared", execute)
    with closing(service._prepare_jit_items(declarations, None)) as items:
        for item in items:
            service._execute_prepared(item)
    expected = [item.canonical_paper_key for item in bundle.paper_work_items]
    assert prepared == calls == expected
    assert len({id(proof) for proof in proofs}) == 1
    assert proofs[0]._active is False

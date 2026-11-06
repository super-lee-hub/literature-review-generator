"""A startup handoff may reuse proofs, but never changed source bytes."""
from dataclasses import replace
from pathlib import Path
import os

import pytest

from runtime import explicit_stage1_scope as scopes
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from tests.test_stage1_explicit_typed_import import _generated_import_fixture
from tests.test_runtime_bridge_helpers import current_config


@pytest.fixture()
def case(tmp_path, monkeypatch):
    row, bundle, _owner = _generated_import_fixture(tmp_path / "source")
    config = current_config(tmp_path)
    monkeypatch.setattr(scopes, "build_source_bundle_for_request", lambda *a, **k: bundle)
    spec = RuntimeJobSpec(
        project_name=bundle.project_name, action="run_all", config=str(config),
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path / "source")),
        reuse_stage1=True,
        reuse_summary_files=(row["stage1_reuse"]["typed_manifest_path"],),
        metadata={"stage1_summary_input_mode": "explicit_typed_import"},
    )
    return spec


def test_one_verification_and_content_checked_handoff(case, monkeypatch):
    calls = []
    original = scopes._resolve_with_config
    def counted(*a, **k):
        calls.append(1)
        return original(*a, **k)
    monkeypatch.setattr(scopes, "_resolve_with_config", counted)
    first = scopes.load_explicit_stage1_scope(case, allow_template_credentials=True)
    second = scopes.load_explicit_stage1_scope(case, verified_scope=first,
                                              allow_template_credentials=True)
    assert len(calls) == 1
    assert second.import_plan.authority_set_hash == first.import_plan.authority_set_hash
    assert second is not first and second.config is not first.config
    # A separate startup has no inherited scope and must verify afresh.
    scopes.load_explicit_stage1_scope(case, allow_template_credentials=True)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["pdf", "manifest", "summary", "config"])
def test_handoff_rejects_changed_bytes_even_with_unchanged_size_and_mtime(case, kind):
    scope = scopes.load_explicit_stage1_scope(case, allow_template_credentials=True)
    entry = scope.import_plan.imports[0]
    target = Path({"pdf": entry.current_source_pdf_path,
                   "manifest": entry.authority.manifest_path,
                   "summary": entry.authority.source_summary_path,
                   "config": case.config}[kind])
    stat = target.stat()
    data = target.read_bytes()
    target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(scopes.ExplicitStage1ScopeError, match="bytes changed"):
        scopes.load_explicit_stage1_scope(case, verified_scope=scope,
                                          allow_template_credentials=True)


def test_handoff_rejects_discovery_and_scope_changes(case):
    scope = scopes.load_explicit_stage1_scope(case, allow_template_credentials=True)
    with pytest.raises(scopes.ExplicitStage1ScopeError, match="identity changed"):
        scopes.load_explicit_stage1_scope(replace(case, action="analyze"), verified_scope=scope,
                                          allow_template_credentials=True)
    (Path(case.source.pdf_folder) / "new.pdf").write_bytes(b"new source")
    with pytest.raises(scopes.ExplicitStage1ScopeError, match="identity changed"):
        scopes.load_explicit_stage1_scope(case, verified_scope=scope,
                                          allow_template_credentials=True)

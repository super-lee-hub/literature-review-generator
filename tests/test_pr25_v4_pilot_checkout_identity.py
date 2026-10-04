"""Authorization identity regressions for the bounded V4 topic pilot.

The route is labeled as an external chat-completions endpoint, but the test
transport is a local fixture and performs no network I/O.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import outline.v3_executor as executor_module
from runtime.checkout_identity import read_checkout_sha
from runtime.provider_runtime import bind_acceptance_execution_context, hash_json
from tests.test_pr25_v4_pilot_integration import (
    _acceptance_binding,
    _executor,
    _pilot,
    _route,
    _LocalTopicRouter,
)
from tests.test_outline_v3_semantic_execution import _summary


def _git_head(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _tiny_git_checkout(root: Path) -> tuple[Path, Path, str]:
    root.mkdir()
    source_file = root / "runtime_source.py"
    source_file.write_text("SOURCE_VERSION = 'clean'\n", encoding="utf-8")
    commands = [
        ["git", "init"],
        ["git", "config", "user.name", "Offline Pilot Test"],
        ["git", "config", "user.email", "offline-pilot@example.invalid"],
        ["git", "add", "runtime_source.py"],
        ["git", "commit", "-m", "fixture source identity"],
    ]
    for command in commands:
        subprocess.run(
            command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    return root, source_file, _git_head(root)


def _external_pilot_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_checkout: Path,
    source_sha: str,
    case_name: str,
):
    acceptance_run_id = f"checkout-identity-{case_name}"
    transport = _LocalTopicRouter()
    summaries = [
        _summary("paper-a", "Study A", "The treatment improved the outcome."),
        _summary("paper-b", "Study B", "The effect held under a second context."),
    ]
    route = _route(transport, endpoint_type="chat_completions")
    pilot = _pilot(summaries, route, acceptance_run_id=acceptance_run_id)
    # Request-hash materialization is a provider-free fixture setup step. The
    # clean/dirty checkout behavior is tested on the resulting real executor.
    with monkeypatch.context() as probe_patch:
        probe_patch.setattr(
            executor_module,
            "read_checkout_sha",
            lambda _root, *, require_clean: "c1ad0da869bc68869a521f60bbda07342fb16058",
        )
        executor, _registry = _executor(
            tmp_path,
            transport=transport,
            route=route,
            pilot=pilot,
        )
    context, controller = _acceptance_binding(tmp_path, acceptance_run_id)
    context = replace(context, final_executable_sha=source_sha)

    def read_isolated_source_sha(
        _executor_root: str | Path,
        *,
        require_clean: bool = False,
    ) -> str:
        return read_checkout_sha(source_checkout, require_clean=require_clean)

    monkeypatch.setattr(executor_module, "read_checkout_sha", read_isolated_source_sha)
    return executor, transport, context, controller, pilot


def test_owner_authorized_external_pilot_rejects_stale_executable_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_checkout, _source_file, source_sha = _tiny_git_checkout(
        tmp_path / "stale-source"
    )
    executor, transport, context, controller, pilot = _external_pilot_case(
        tmp_path / "stale-run",
        monkeypatch,
        source_checkout=source_checkout,
        source_sha=source_sha,
        case_name="stale-sha",
    )
    stale_sha = "0" * 40 if source_sha != "0" * 40 else "f" * 40
    context = replace(context, final_executable_sha=stale_sha)

    # Establish that identity is the only deliberately invalid authority field:
    # pilot source and route match, run IDs match, the budget/context match, and
    # the owner is explicitly authorized for this external-looking route.
    route = executor._role_route("candidate_1_provider_generation")
    assert route.endpoint_type == "chat_completions"
    assert route.safe_config_fingerprint() == pilot["allowed_route_fingerprint"]
    assert pilot["source_summary_set_hash"] == hash_json(executor.summaries)
    assert context.acceptance_run_id == pilot["acceptance_run_id"]
    assert context.owner_authorized is True
    assert controller.budget == context.provider_budget
    assert context.final_executable_sha != source_sha

    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    # A current-source identity check must block before any simulated POST.
    assert transport.calls == []
    assert result.status == "blocked"
    assert any(
        "source" in diagnostic.casefold()
        or "executable" in diagnostic.casefold()
        for diagnostic in result.diagnostics
    )


def test_external_pilot_accepts_matching_clean_checkout_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_checkout, _source_file, source_sha = _tiny_git_checkout(
        tmp_path / "clean-source"
    )
    executor, transport, context, controller, pilot = _external_pilot_case(
        tmp_path / "clean-run",
        monkeypatch,
        source_checkout=source_checkout,
        source_sha=source_sha,
        case_name="clean-control",
    )

    assert context.owner_authorized is True
    assert context.final_executable_sha == source_sha
    assert executor._pilot_allowed_node_ids == frozenset()
    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert result.status == "topic_pilot_complete", result.diagnostics
    assert transport.calls == pilot["selected_topic_batch_ids"]


def test_external_pilot_blocks_matching_head_when_source_checkout_is_dirty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_checkout, source_file, source_sha = _tiny_git_checkout(
        tmp_path / "dirty-source"
    )
    source_file.write_text("SOURCE_VERSION = 'modified-but-uncommitted'\n", encoding="utf-8")
    executor, transport, context, controller, _pilot_manifest = _external_pilot_case(
        tmp_path / "dirty-run",
        monkeypatch,
        source_checkout=source_checkout,
        source_sha=source_sha,
        case_name="dirty-source",
    )

    assert context.owner_authorized is True
    assert context.final_executable_sha == _git_head(source_checkout)
    assert context.final_executable_sha == source_sha
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=source_checkout,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    with bind_acceptance_execution_context(context, controller):
        result = executor.run()

    assert transport.calls == []
    assert result.status == "blocked"

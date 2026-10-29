"""SourceRevision through normal Runner, real loopback transport and adoption."""
from dataclasses import replace
import configparser
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from tests.test_run_first_scale_integration import (
    _local_config, _scale_papers, _start_scale_server, _write_scale_pdf,
)


def _adopt_and_finish(result, control):
    assert result.failed_stage is None, result.message
    workspace, registry = AgentRuntimeRunner._open_workspace(result.workspace_path)
    outline = registry.get("outline-v3:final_outline")
    assert outline is not None
    adoption = control.adopt(
        workspace=result.workspace_path, artifact_id=outline.artifact_id,
        actor="tests.source_revision_runner_http", reason="approve local engineering outline",
        expected_hash=outline.content_hash,
    )
    assert adoption["status"] == "succeeded", adoption
    completed = control.resume(workspace=result.workspace_path)
    assert completed["canonical_ready"] is True, completed
    assert completed["completion_status"] == "complete", completed
    return workspace, registry


@pytest.mark.parametrize("topic_target", [0, 8000], ids=["default", "topic-shards"])
def test_source_revision_runner_http_consumes_plan_and_preserves_parent(tmp_path, topic_target):
    papers = _scale_papers(4)
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for paper in papers:
        _write_scale_pdf(old / f"{paper.stem}.pdf", paper,
                         identity_doi=f"10.1234/source-revision-{paper.index}")
        shutil.copyfile(old / f"{paper.stem}.pdf", new / f"{paper.stem}.pdf")
    server, thread, api_base = _start_scale_server(papers)
    try:
        config = _local_config(
            tmp_path, api_base, provider_call_limit=200,
            technical_shard_target_tokens=topic_target,
        )
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config, encoding="utf-8")
        # Exercise normal route-sized requests; only the topic shard soft
        # target varies, so global aggregation retains its available window.
        parser["OutlineStability"]["max_source_prompt_tokens"] = "0"
        with config.open("w", encoding="utf-8") as stream:
            parser.write(stream)
        metadata = {
            "provider_semantic_contract_version": "source-closed-provider/v2",
            "review_intent": {
                "question": "Compare source findings and preserve their conditions",
                "required_comparisons": [{
                    "paper_keys": [papers[0].paper_key, other.paper_key],
                    "question": "Compare the source findings within their reported sample boundaries",
                    "relation_type": "reported_magnitude_contrast",
                } for other in papers[1:]],
            },
            "provider_budget": {
                "schema_version": "provider-aggregate-budget-contract-v2",
                "max_provider_calls_total": 200, "max_output_tokens_total": 4_000_000,
                "max_retry_attempts_total": 0, "max_wall_seconds": 1800,
            },
        }
        parent_spec = RuntimeJobSpec(
            project_name="revision-http", job_id="parent-http", action="run_all",
            config=str(config), source=RuntimeSourceSpec(mode="direct", pdf_folder=str(old)),
            queue_file=str(tmp_path / "queue.json"), metadata=metadata,
        )
        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        parent_ws, parent_registry = _adopt_and_finish(AgentRuntimeRunner(parent_spec).run(), control)
        parent_files = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(parent_ws.root_dir).rglob("*") if path.is_file()
        }
        revised = replace(papers[-1], finding=papers[-1].finding + " Only adult participants were enrolled.")
        _write_scale_pdf(new / f"{revised.stem}.pdf", revised,
                         identity_doi=f"10.1234/source-revision-{revised.index}")
        server.papers = [*papers[:-1], revised]
        child_spec = replace(
            parent_spec, job_id="child-http", source=RuntimeSourceSpec(mode="direct", pdf_folder=str(new)),
            workspace_path=str(Path(parent_ws.root_dir).parent / "revision-http__child-http"),
        )
        spec_path = tmp_path / "child.json"
        spec_path.write_text(json.dumps(child_spec.to_dict()), encoding="utf-8")
        planned = control.source_revision_plan(
            spec_path, parent_workspace=Path(parent_ws.root_dir), output_path=tmp_path / "revision-plan.json",
        )
        child_spec = RuntimeJobSpec.from_mapping(planned["normalized_child_spec"])
        before = len(server.calls)
        first_child = AgentRuntimeRunner(child_spec).run()
        assert first_child.failed_stage is None, first_child.message
        child_ws, child_registry = _adopt_and_finish(first_child, control)
        child_registry.reload()
        assert child_registry.get("source_revision_plan") is not None
        proofs = [r for r in child_registry.list_records() if r.artifact_type == "source_revision_provider_reuse"]
        assert proofs, "normal child execution did not consume parent provider results"
        if topic_target:
            assert any(record.metadata.get("stage_name") == "outline_v3" for record in proofs), "topic-sharded Runner did not reuse an eligible Outline response"
        assert len(server.calls) > before, "changed source work must still execute"
        sent = len(server.calls)
        again = control.resume(workspace=child_ws.root_dir)
        assert again["canonical_ready"] is True, again
        assert len(server.calls) == sent
        exported = control.export(workspace=child_ws.root_dir)
        bundle = Path(exported["bundle_path"])
        bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
        with zipfile.ZipFile(bundle) as archive:
            assert archive.testzip() is None
            for name, expected in json.loads(archive.read("checksums.json")).items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        repeated = control.export(workspace=child_ws.root_dir)
        assert repeated["bundle_id"] == exported["bundle_id"]
        assert hashlib.sha256(Path(repeated["bundle_path"]).read_bytes()).hexdigest() == bundle_hash
        assert len(server.calls) == sent
        assert {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in parent_files} == parent_files
        assert not server.errors, server.errors
        assert not server.transport_errors, server.transport_errors
        assert parent_registry.job_id != child_registry.job_id
    finally:
        (tmp_path / "http-call-trace.json").write_text(
            json.dumps({"calls": server.calls, "errors": server.errors}, default=str), encoding="utf-8",
        )
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from reviewctl import main
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import publish_json_artifact
from services.queue_service import LocalPublicationContext
from services.summary_correction import verify_source_summary_correction_candidate
from tests.test_summary_correction import _build_source, _proposal_payload


def _input(tmp_path: Path):
    workspace, registry, record, summaries, pdf, pdf_hash, renders = _build_source(tmp_path)
    proposal = _proposal_payload(record, summaries, pdf, pdf_hash, renders)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    return workspace, registry, record, proposal, proposal_path


def test_public_source_correction_plan_publishes_only_quarantined_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, registry, record, _proposal, proposal_path = _input(tmp_path)
    registry_before = Path(registry.registry_path).read_bytes()
    source_before = Path(record.path).read_bytes()
    assert main([
        "source-correction-plan", "--workspace", source.root_dir,
        "--proposal", str(proposal_path), "--output-root", str(tmp_path / "corrections"),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready_for_owner_review"
    assert output["requires_owner_approval"] is True
    assert output["usable_as_stage1_reuse"] is False
    assert output["canonical_pointer_advanced"] is False
    assert output["provider_posts"] == 0
    assert Path(registry.registry_path).read_bytes() == registry_before
    assert Path(record.path).read_bytes() == source_before
    destination_path = Path(output["destination_workspace"])
    assert not destination_path.is_relative_to(Path(source.root_dir))
    destination = ArtifactRegistry(destination_path / "artifact_registry.json", destination_path.name.split("__", 1)[1])
    candidate = destination.get(output["candidate_artifact_id"])
    assert candidate is not None and candidate.status == "quarantined"
    verification = verify_source_summary_correction_candidate(
        source_registry=registry, destination_registry=destination,
        candidate_artifact_id=candidate.artifact_id,
    )
    assert verification.verified and verification.identity_set_preserved
    assert hashlib.sha256(Path(record.path).read_bytes()).hexdigest() == record.content_hash


@pytest.mark.parametrize("tampered", [False, True])
def test_public_source_correction_inspection_rechecks_exact_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tampered: bool,
) -> None:
    source, registry, record, _proposal, proposal_path = _input(tmp_path)
    assert main([
        "source-correction-plan", "--workspace", source.root_dir,
        "--proposal", str(proposal_path), "--output-root", str(tmp_path / "corrections"),
    ]) == 0
    preparation = json.loads(capsys.readouterr().out)
    destination_path = Path(preparation["destination_workspace"])
    destination = ArtifactRegistry(destination_path / "artifact_registry.json", destination_path.name.split("__", 1)[1])
    candidate = destination.get(preparation["candidate_artifact_id"])
    assert candidate is not None
    if tampered:
        Path(candidate.path).write_bytes(Path(candidate.path).read_bytes() + b" ")
    before = [Path(path).read_bytes() for path in (
        registry.registry_path, destination.registry_path, record.path,
    )]
    code = main([
        "source-correction-inspect", "--source-workspace", source.root_dir,
        "--workspace", str(destination_path), "--candidate", candidate.artifact_id,
    ])
    result = json.loads(capsys.readouterr().out)
    assert code == (1 if tampered else 0)
    assert result["status"] == ("blocked" if tampered else "ready_for_owner_review")
    assert result["read_only"] and result["provider_posts"] == 0
    assert result["usable_as_stage1_reuse"] is False
    if not tampered:
        assert result["candidate_artifact_hash"] == candidate.content_hash
        assert result["summary_count"] == 63
        assert result["field_changes"]
        assert result["registries_unchanged"]
    assert before == [Path(path).read_bytes() for path in (
        registry.registry_path, destination.registry_path, record.path,
    )]


@pytest.mark.parametrize("invalid", [None, "wrong_hash", "missing_actor", "missing_reason"])
def test_public_source_correction_adoption_requires_exact_explicit_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], invalid: str | None,
) -> None:
    source, registry, record, _proposal, proposal_path = _input(tmp_path)
    assert main([
        "source-correction-plan", "--workspace", source.root_dir,
        "--proposal", str(proposal_path), "--output-root", str(tmp_path / "corrections"),
    ]) == 0
    preparation = json.loads(capsys.readouterr().out)
    destination_path = Path(preparation["destination_workspace"])
    destination = ArtifactRegistry(destination_path / "artifact_registry.json", destination_path.name.split("__", 1)[1])
    candidate = destination.get(preparation["candidate_artifact_id"])
    assert candidate is not None
    original_bytes = Path(registry.registry_path).read_bytes(), Path(record.path).read_bytes()
    destination_before = Path(destination.registry_path).read_bytes()
    arguments = [
        "source-correction-adopt", "--source-workspace", source.root_dir,
        "--workspace", str(destination_path), "--candidate", candidate.artifact_id,
        "--expected-hash", "0" * 64 if invalid == "wrong_hash" else candidate.content_hash,
        "--actor", "" if invalid == "missing_actor" else "synthetic-test-owner",
        "--reason", "" if invalid == "missing_reason" else "Reviewed synthetic source correction.",
    ]
    code = main(arguments)
    result = json.loads(capsys.readouterr().out)
    assert result["provider_posts"] == 0 and result["usable_as_stage1_reuse"] is False
    assert result["canonical_pointer_advanced"] is False
    assert original_bytes == (Path(registry.registry_path).read_bytes(), Path(record.path).read_bytes())
    destination.reload()
    assert destination.get(candidate.artifact_id).status == "quarantined"
    if invalid:
        assert code == 1 and result["status"] == "blocked"
        assert Path(destination.registry_path).read_bytes() == destination_before
    else:
        assert code == 0 and result["status"] == "owner_approved_derived_summary_ready", result
        assert result["summary_count"] == 63
        assert result["source_registry_unchanged"]
        after = Path(destination.registry_path).read_bytes()
        assert main(arguments) == 0
        repeated = json.loads(capsys.readouterr().out)
        assert repeated["adoption_receipt_artifact_id"] == result["adoption_receipt_artifact_id"]
        assert repeated["derived_summary_artifact_hash"] == result["derived_summary_artifact_hash"]
        assert Path(destination.registry_path).read_bytes() == after


def test_public_source_correction_accepts_matching_ready_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, registry, record, _proposal, proposal_path = _input(tmp_path)
    publish_json_artifact(
        LocalPublicationContext(), registry, source.artifact_path("ready_stage1_pointer.json"),
        json.loads(Path(record.path).read_text(encoding="utf-8")),
        artifact_id="stage1_summaries", artifact_role="stage1_input",
        artifact_type="stage1_canonical_summaries", artifact_version="v1",
        producer="tests.test_reviewctl_source_correction",
        metadata={"current_version_artifact_id": record.artifact_id},
    )
    before = Path(registry.registry_path).read_bytes()
    assert main([
        "source-correction-plan", "--workspace", source.root_dir,
        "--proposal", str(proposal_path), "--output-root", str(tmp_path / "corrections"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["source_registry_unchanged"]
    assert Path(registry.registry_path).read_bytes() == before


@pytest.mark.parametrize("invalid", ["stale_binding", "output_inside_source", "nonready_pointer"])
def test_public_source_correction_rejects_unsafe_or_stale_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], invalid: str,
) -> None:
    source, registry, record, proposal, proposal_path = _input(tmp_path)
    output_root = tmp_path / "corrections"
    if invalid == "stale_binding":
        proposal["source_authority"]["summary_set_hash"] = "0" * 64
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    elif invalid == "output_inside_source":
        output_root = Path(source.root_dir) / "corrections"
    else:
        publish_json_artifact(
            LocalPublicationContext(), registry,
            source.artifact_path("nonready_stage1_pointer.json"),
            json.loads(Path(record.path).read_text(encoding="utf-8")),
            artifact_id="stage1_summaries", artifact_role="stage1_input",
            artifact_type="stage1_canonical_summaries", artifact_version="v1",
            producer="tests.test_reviewctl_source_correction", status="quarantined",
            metadata={"current_version_artifact_id": record.artifact_id},
        )
    before = Path(registry.registry_path).read_bytes(), Path(record.path).read_bytes()
    assert main([
        "source-correction-plan", "--workspace", source.root_dir,
        "--proposal", str(proposal_path), "--output-root", str(output_root),
    ]) != 0
    assert json.loads(capsys.readouterr().out)["status"] in {"blocked", "error"}
    assert before == (Path(registry.registry_path).read_bytes(), Path(record.path).read_bytes())

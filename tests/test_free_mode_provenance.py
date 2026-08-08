from pathlib import Path

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from services.job_runner import (
    JobRunRequest,
    JobRunner,
    build_job_request_from_mapping,
    validate_job_request_options,
)


def _spec(**kwargs: object) -> RuntimeJobSpec:
    return RuntimeJobSpec(
        project_name="demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="papers"),
        config="config.ini",
        action="analyze",
        **kwargs,
    )


def test_free_mode_options_round_trip_through_spec_and_mapping(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text('{"style":"concise"}', encoding="utf-8")
    spec = _spec(free_mode_profile=str(profile))

    payload = spec.to_dict()
    restored = RuntimeJobSpec.from_dict(payload)
    request = restored.to_job_request()
    mapped = RuntimeJobSpec.from_mapping(
        {
            "project_name": "demo",
            "pdf_folder": "papers",
            "free_mode_profile": str(profile),
            "action": "analyze",
        }
    ).to_job_request()

    assert payload["free_mode_profile"] == str(profile)
    assert request.free_mode_profile == str(profile)
    assert request.free_mode_idea is None
    assert mapped.free_mode_profile == str(profile)


def test_free_mode_profile_and_idea_are_rejected(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder="papers",
        action="analyze",
        free_mode_profile=str(profile),
        free_mode_idea="an idea",
    )

    assert "mutually exclusive" in (validate_job_request_options(request) or "")
    with pytest.raises(ValueError, match="mutually exclusive"):
        _spec(free_mode_profile=str(profile), free_mode_idea="an idea").validate()


def test_missing_or_directory_profile_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    directory = tmp_path / "profile-dir"
    directory.mkdir()

    for profile in (missing, directory):
        request = JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder="papers",
            action="analyze",
            free_mode_profile=str(profile),
        )
        assert "does not exist or is not a file" in (validate_job_request_options(request) or "")
        with pytest.raises(ValueError, match="does not exist or is not a file"):
            _spec(free_mode_profile=str(profile)).validate()


def test_free_mode_content_changes_request_provenance(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text("profile-v1", encoding="utf-8")
    runner = JobRunner()
    profile_request = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder="papers",
        action="analyze",
        free_mode_profile=str(profile),
    )
    first = runner._request_snapshot(profile_request)
    profile.write_text("profile-v2", encoding="utf-8")
    second = runner._request_snapshot(profile_request)

    assert first["free_mode_profile"] == second["free_mode_profile"]
    assert first["free_mode_profile_sha256"] != second["free_mode_profile_sha256"]

    first_idea = runner._request_snapshot(
        JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder="papers",
            action="analyze",
            free_mode_idea="idea-v1",
        )
    )
    second_idea = runner._request_snapshot(
        JobRunRequest(
            config="config.ini",
            project_name="demo",
            pdf_folder="papers",
            action="analyze",
            free_mode_idea="idea-v2",
        )
    )
    assert first_idea["free_mode_idea_sha256"] != second_idea["free_mode_idea_sha256"]


def test_mapping_preserves_free_mode_options() -> None:
    request = build_job_request_from_mapping(
        {
            "project_name": "demo",
            "pdf_folder": "papers",
            "free_mode_idea": "draft idea",
        }
    )
    assert request.free_mode_idea == "draft idea"
    assert request.free_mode_profile is None

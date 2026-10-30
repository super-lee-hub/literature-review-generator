from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec


def _valid_pilot() -> dict[str, Any]:
    return {
        "schema_version": "outline-topic-pilot/v1",
        "selected_topic_batch_ids": [
            "topic_synthesis_provider:batch:1",
            "topic_synthesis_provider:batch:2",
            "topic_synthesis_provider:batch:3",
        ],
        "selected_request_hashes": {
            "topic_synthesis_provider:batch:1": "1" * 64,
            "topic_synthesis_provider:batch:2": "2" * 64,
            "topic_synthesis_provider:batch:3": "3" * 64,
        },
        "source_summary_set_hash": "a" * 64,
        "allowed_route_fingerprint": "b" * 64,
        "acceptance_run_id": "acceptance-run-v4-pilot",
        "max_physical_attempts": 12,
        "max_output_tokens_all_attempts": 24_000,
        "deadline_utc": "2026-10-01T10:00:00Z",
        "auto_continue": False,
        "adoption_authorized": False,
    }


def _spec(
    *,
    action: str = "generate_outline",
    requested_stages: Any = ("outline",),
    pilot: Any = None,
) -> RuntimeJobSpec:
    metadata: dict[str, Any] = {"requested_stages": requested_stages}
    if pilot is not None:
        metadata["outline_pilot"] = pilot
    return RuntimeJobSpec(
        project_name="pilot-spec-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="C:/frozen-papers"),
        action=action,
        metadata=metadata,
    )


def test_outline_topic_pilot_spec_accepts_strict_outline_only_contract() -> None:
    pilot = _valid_pilot()
    spec = _spec(pilot=pilot)

    spec.validate()
    round_tripped = RuntimeJobSpec.from_dict(spec.to_dict())

    assert round_tripped.metadata["outline_pilot"] == pilot
    assert len(round_tripped.metadata["outline_pilot"]["selected_request_hashes"]) == 3
    assert round_tripped.to_job_request().requested_stages == ("outline",)


def test_outline_topic_pilot_allows_source_intake_before_outline() -> None:
    spec = _spec(
        requested_stages=["source_intake", "outline"],
        pilot=_valid_pilot(),
    )

    spec.validate()
    assert spec.to_job_request().requested_stages == ("source_intake", "outline")


def test_non_pilot_generate_outline_spec_keeps_existing_behavior() -> None:
    spec = RuntimeJobSpec(
        project_name="ordinary-outline",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="C:/papers"),
        action="generate_outline",
    )

    spec.validate()

    assert spec.to_job_request().generate_outline is True


def test_outline_topic_pilot_rejects_unknown_and_missing_fields() -> None:
    unknown = _valid_pilot()
    unknown["extra"] = "ignored fields are not safe"
    with pytest.raises(ValueError, match="outline_pilot contains unknown fields: extra"):
        _spec(pilot=unknown).validate()

    wrong_version = _valid_pilot()
    wrong_version["schema_version"] = "outline-topic-pilot/v2"
    with pytest.raises(ValueError, match="outline_pilot schema_version is invalid"):
        _spec(pilot=wrong_version).validate()

    missing = _valid_pilot()
    missing.pop("acceptance_run_id")
    with pytest.raises(ValueError, match="outline_pilot is missing required fields: acceptance_run_id"):
        _spec(pilot=missing).validate()

    missing_hashes = _valid_pilot()
    missing_hashes.pop("selected_request_hashes")
    with pytest.raises(
        ValueError,
        match="outline_pilot is missing required fields: selected_request_hashes",
    ):
        _spec(pilot=missing_hashes).validate()


@pytest.mark.parametrize(
    ("action", "stages"),
    [
        ("run_all", ["outline"]),
        ("generate_outline", None),
        ("generate_outline", []),
        ("generate_outline", ["analyze", "outline"]),
        ("generate_outline", ["outline", "review"]),
        ("generate_outline", ["source_intake", "outline", "validate"]),
        ("generate_outline", ["outline", "source_intake"]),
        ("generate_outline", ["source_intake", "outline", "outline"]),
    ],
)
def test_outline_topic_pilot_requires_generate_outline_and_stops_at_outline(
    action: str,
    stages: Any,
) -> None:
    with pytest.raises(ValueError, match="outline_pilot requires"):
        _spec(action=action, requested_stages=stages, pilot=_valid_pilot()).validate()


@pytest.mark.parametrize(
    "batch_ids",
    [
        [],
        ["topic_synthesis_provider:batch:1", "topic_synthesis_provider:batch:1"],
        ["topic_synthesis_provider:batch:0"],
        ["topic_synthesis_provider:batch:01"],
        ["cross_group_comparison_provider:batch:1"],
        [1],
        "topic_synthesis_provider:batch:1",
    ],
)
def test_outline_topic_pilot_requires_nonempty_unique_topic_batch_ids(
    batch_ids: Any,
) -> None:
    pilot = _valid_pilot()
    pilot["selected_topic_batch_ids"] = batch_ids

    with pytest.raises(ValueError, match="selected_topic_batch_ids"):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize(
    "request_hashes",
    [
        {
            "topic_synthesis_provider:batch:1": "1" * 64,
            "topic_synthesis_provider:batch:2": "2" * 64,
        },
        {
            "topic_synthesis_provider:batch:1": "1" * 64,
            "topic_synthesis_provider:batch:2": "2" * 64,
            "topic_synthesis_provider:batch:3": "3" * 64,
            "topic_synthesis_provider:batch:4": "4" * 64,
        },
    ],
    ids=["missing-batch-hash", "extra-batch-hash"],
)
def test_outline_topic_pilot_request_hash_keys_exactly_match_selected_batches(
    request_hashes: dict[str, str],
) -> None:
    pilot = _valid_pilot()
    pilot["selected_request_hashes"] = request_hashes

    with pytest.raises(
        ValueError,
        match="selected_request_hashes keys must exactly match selected_topic_batch_ids",
    ):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize(
    "request_hash",
    ["not-a-hash", "A" * 64, "a" * 63, 123],
    ids=["not-hex", "uppercase", "short", "not-string"],
)
def test_outline_topic_pilot_request_hash_values_are_lowercase_sha256(
    request_hash: Any,
) -> None:
    pilot = _valid_pilot()
    pilot["selected_request_hashes"]["topic_synthesis_provider:batch:2"] = request_hash

    with pytest.raises(
        ValueError,
        match="selected_request_hashes values must be lowercase SHA-256 hashes",
    ):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source_summary_set_hash", "not-a-hash"),
        ("source_summary_set_hash", "A" * 64),
        ("allowed_route_fingerprint", "f" * 63),
        ("allowed_route_fingerprint", 123),
    ],
)
def test_outline_topic_pilot_requires_lowercase_sha256_hashes(
    field_name: str,
    value: Any,
) -> None:
    pilot = _valid_pilot()
    pilot[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize("acceptance_run_id", ["", "   ", 4, None])
def test_outline_topic_pilot_requires_nonempty_acceptance_run_id(
    acceptance_run_id: Any,
) -> None:
    pilot = _valid_pilot()
    pilot["acceptance_run_id"] = acceptance_run_id

    with pytest.raises(ValueError, match="acceptance_run_id"):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_physical_attempts", 0),
        ("max_physical_attempts", -1),
        ("max_physical_attempts", True),
        ("max_physical_attempts", "12"),
        ("max_output_tokens_all_attempts", 0),
        ("max_output_tokens_all_attempts", -1),
        ("max_output_tokens_all_attempts", False),
        ("max_output_tokens_all_attempts", "24000"),
    ],
)
def test_outline_topic_pilot_requires_positive_integer_budgets(
    field_name: str,
    value: Any,
) -> None:
    pilot = _valid_pilot()
    pilot[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize(
    "deadline_utc",
    [
        "2026-10-01T10:00:00",
        "2026-10-01T10:00:00+01:00",
        "2026-13-01T10:00:00Z",
        "not-a-timestamp",
        1_798_830_000,
    ],
)
def test_outline_topic_pilot_requires_timezone_aware_utc_deadline(
    deadline_utc: Any,
) -> None:
    pilot = _valid_pilot()
    pilot["deadline_utc"] = deadline_utc

    with pytest.raises(ValueError, match="deadline_utc"):
        _spec(pilot=pilot).validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("auto_continue", True),
        ("auto_continue", 0),
        ("adoption_authorized", True),
        ("adoption_authorized", "false"),
    ],
)
def test_outline_topic_pilot_forbids_auto_continue_and_adoption_authority(
    field_name: str,
    value: Any,
) -> None:
    pilot = deepcopy(_valid_pilot())
    pilot[field_name] = value

    with pytest.raises(ValueError, match=f"{field_name} must be false"):
        _spec(pilot=pilot).validate()

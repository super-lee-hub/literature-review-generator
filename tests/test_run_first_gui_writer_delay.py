from __future__ import annotations

import argparse
from typing import Any

import pytest

from tests.run_first_gui_delay import (
    DEFAULT_WRITER_RESPONSE_DELAY_NUMBER,
    add_writer_response_delay_argument,
    advance_writer_response_delay_gate,
    parse_writer_response_delay_number,
    validate_writer_response_delay_number,
    writer_response_delay_trace_snapshot,
)


def test_delay_gate_counts_only_stage3_writer_http_200_and_delays_target_once() -> None:
    target_number = 2
    response_count = 0
    delay_fired = False
    decisions: list[bool] = []
    counts: list[int] = []
    events = (
        (200, False),  # non-Writer HTTP success
        (500, True),  # Writer error response
        (200, False),  # another non-Writer HTTP success
        (200, True),  # matching Stage3 Writer response #1
        (200, True),  # matching Stage3 Writer response #2
        (200, True),  # later matching Writer success must not delay again
        (200, True),
    )

    for status_code, is_stage3_writer in events:
        response_count, should_delay = advance_writer_response_delay_gate(
            response_count,
            status_code=status_code,
            is_stage3_writer=is_stage3_writer,
            target_number=target_number,
            delay_fired=delay_fired,
        )
        if should_delay:
            delay_fired = True
        decisions.append(should_delay)
        counts.append(response_count)

    assert counts == [0, 0, 0, 1, 2, 3, 4]
    assert decisions == [False, False, False, False, True, False, False]


def test_cli_delay_number_defaults_to_one_and_accepts_selected_index() -> None:
    parser = argparse.ArgumentParser()
    add_writer_response_delay_argument(parser)

    default_args = parser.parse_args([])
    selected_args = parser.parse_args(["--writer-response-delay-number", "2"])

    assert DEFAULT_WRITER_RESPONSE_DELAY_NUMBER == 1
    assert default_args.writer_response_delay_number == 1
    assert selected_args.writer_response_delay_number == 2


@pytest.mark.parametrize(
    "value",
    (
        0,
        -1,
        True,
        False,
        1.0,
        float("inf"),
        float("nan"),
        None,
        "0",
        "-1",
        "1.5",
        "inf",
        "nan",
        "",
    ),
)
def test_writer_response_delay_number_rejects_invalid_indices(value: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        validate_writer_response_delay_number(value)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "inf", "nan", ""])
def test_cli_writer_response_delay_number_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        parse_writer_response_delay_number(value)


def test_session_metadata_exposes_target_actual_index_and_delay_trace() -> None:
    events = [
        {
            "event": "matching_writer_http_200_response",
            "writer_response_number": 1,
            "target_response_number": 2,
            "delay_applied": False,
            "observed_at": "2026-10-17T00:00:00Z",
        },
        {
            "event": "matching_writer_http_200_response",
            "writer_response_number": 2,
            "target_response_number": 2,
            "delay_applied": True,
            "observed_at": "2026-10-17T00:00:01Z",
        },
        {
            "event": "target_response_delay_started",
            "writer_response_number": 2,
            "target_response_number": 2,
            "delay_seconds": 45.0,
            "observed_at": "2026-10-17T00:00:01Z",
        },
        {
            "event": "target_response_delay_completed",
            "writer_response_number": 2,
            "target_response_number": 2,
            "observed_at": "2026-10-17T00:00:46Z",
        },
    ]
    trace = writer_response_delay_trace_snapshot(
        target_response_number=2,
        actual_delayed_response_number=2,
        matching_http_200_response_count=3,
        delay_fired=True,
        started_at="2026-10-17T00:00:01Z",
        completed_at="2026-10-17T00:00:46Z",
        events=events,
        dropped_events=0,
    )

    assert trace["schema_version"] == "run-first-writer-response-delay-trace/v1"
    assert trace["target_response_number"] == 2
    assert trace["actual_delayed_response_number"] == 2
    assert trace["matching_http_200_response_count"] == 3
    assert trace["delay_fired"] is True
    assert trace["started_at"] == "2026-10-17T00:00:01Z"
    assert trace["completed_at"] == "2026-10-17T00:00:46Z"
    assert trace["events"][1]["writer_response_number"] == 2
    assert trace["events"][1]["delay_applied"] is True
    assert trace["dropped_events"] == 0

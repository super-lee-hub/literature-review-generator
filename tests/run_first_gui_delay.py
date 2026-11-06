"""Pure helpers for the local Run First GUI Writer-delay fixture."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_WRITER_RESPONSE_DELAY_NUMBER = 1
MAX_WRITER_RESPONSE_DELAY_TRACE_EVENTS = 64


def validate_writer_response_delay_number(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("writer response delay number must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdecimal():
        number = int(value.strip())
    else:
        raise ValueError("writer response delay number must be a positive integer")
    if number < 1:
        raise ValueError("writer response delay number must be a positive integer")
    return number


def parse_writer_response_delay_number(value: str) -> int:
    try:
        return validate_writer_response_delay_number(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_writer_response_delay_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--writer-response-delay-number",
        type=parse_writer_response_delay_number,
        default=DEFAULT_WRITER_RESPONSE_DELAY_NUMBER,
        help="one-based matching Stage3 Writer HTTP 200 response number to delay (default: 1)",
    )


def advance_writer_response_delay_gate(
    response_count: int,
    *,
    status_code: int,
    is_stage3_writer: bool,
    target_number: int,
    delay_fired: bool,
) -> tuple[int, bool]:
    target = validate_writer_response_delay_number(target_number)
    if (
        isinstance(response_count, bool)
        or not isinstance(response_count, int)
        or response_count < 0
    ):
        raise ValueError("writer response count must be a non-negative integer")
    if status_code != 200 or is_stage3_writer is not True:
        return response_count, False
    next_count = response_count + 1
    return next_count, not delay_fired and next_count == target


def append_writer_response_delay_trace(
    events: Sequence[Mapping[str, Any]],
    dropped_events: int,
    event: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    copied = [dict(item) for item in events]
    event_name = str(event.get("event") or "")
    is_delay_boundary = event_name in {
        "target_response_delay_started",
        "target_response_delay_completed",
    }
    regular_event_limit = MAX_WRITER_RESPONSE_DELAY_TRACE_EVENTS - 2
    if len(copied) >= MAX_WRITER_RESPONSE_DELAY_TRACE_EVENTS or (
        not is_delay_boundary and len(copied) >= regular_event_limit
    ):
        return copied, max(0, int(dropped_events)) + 1
    copied.append(dict(event))
    return copied, max(0, int(dropped_events))


def writer_response_delay_trace_snapshot(
    *,
    target_response_number: int,
    actual_delayed_response_number: int | None,
    matching_http_200_response_count: int,
    delay_fired: bool,
    started_at: str,
    completed_at: str,
    events: Sequence[Mapping[str, Any]],
    dropped_events: int,
) -> dict[str, Any]:
    return {
        "schema_version": "run-first-writer-response-delay-trace/v1",
        "target_response_number": validate_writer_response_delay_number(
            target_response_number
        ),
        "actual_delayed_response_number": actual_delayed_response_number,
        "matching_http_200_response_count": max(
            0, int(matching_http_200_response_count)
        ),
        "delay_fired": bool(delay_fired),
        "started_at": str(started_at or ""),
        "completed_at": str(completed_at or ""),
        "events": [dict(item) for item in events],
        "dropped_events": max(0, int(dropped_events)),
    }

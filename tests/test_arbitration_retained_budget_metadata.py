"""Original token reserves remain usable without unredacting old receipts."""
from dataclasses import replace

import pytest

from runtime.arbitration_dispatch import (
    ArbitrationDispatchError,
    retained_output_reserve_per_attempt,
)
from tests.test_arbitration_overflow_admission import _retained_receipt_case


def test_real_receipt_preserves_numeric_reserve_and_redacts_credentials(tmp_path):
    _expected, original, _content, _route, _request = _retained_receipt_case(tmp_path)
    receipt = replace(original, metadata={"estimation": {
        "requested_output_tokens_per_attempt": 7421,
        "max_output_tokens": 16000,
        "api_token": "private-test-credential",
    }})
    estimation = receipt.metadata["estimation"]
    assert estimation["requested_output_tokens_per_attempt"] == 7421
    assert estimation["api_token"] == "[REDACTED_SECRET]"
    assert retained_output_reserve_per_attempt(estimation) == 7421
    assert original.metadata.get("estimation") is None


@pytest.mark.parametrize("value", ["private-test-credential", "7421", True, -1])
def test_nonnumeric_or_invalid_accounting_key_is_still_redacted(tmp_path, value):
    _expected, original, _content, _route, _request = _retained_receipt_case(tmp_path)
    receipt = replace(original, metadata={"requested_output_tokens_per_attempt": value})
    assert receipt.metadata["requested_output_tokens_per_attempt"] == "[REDACTED_SECRET]"


def test_legacy_redacted_reserve_uses_original_route_ceiling_without_mutation():
    estimation = {"requested_output_tokens_per_attempt": "[REDACTED_SECRET]", "max_output_tokens": 16000}
    assert retained_output_reserve_per_attempt(estimation) == 16000
    assert estimation["requested_output_tokens_per_attempt"] == "[REDACTED_SECRET]"


@pytest.mark.parametrize("estimation", [
    {}, {"requested_output_tokens_per_attempt": "[REDACTED_SECRET]"},
    {"requested_output_tokens_per_attempt": "wrong", "max_output_tokens": 16000},
    {"requested_output_tokens_per_attempt": False, "max_output_tokens": 16000},
    {"requested_output_tokens_per_attempt": 0, "max_output_tokens": 16000},
    {"requested_output_tokens_per_attempt": -1, "max_output_tokens": 16000},
    {"max_output_tokens": "[REDACTED_SECRET]"},
])
def test_missing_or_invalid_prior_exposure_fails_closed(estimation):
    with pytest.raises(ArbitrationDispatchError, match="unavailable or invalid"):
        retained_output_reserve_per_attempt(estimation)

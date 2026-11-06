"""Equivalence and visit-count tests for critical-view fact partitioning."""

from __future__ import annotations

import copy

import pytest

from outline.candidate_arbitration_views import (
    CRITICAL_VIEW_SCHEMA,
    ArbitrationViewError,
    _validate_fact_partition,
    validate_arbitration_view_set,
)
from outline.v3_models import compute_v3_hash
from tests.test_run_first_arbitration_aggregation import _build_ready


class _CountingRow(dict):
    def __init__(self, row):
        super().__init__(row)
        self.claim_slot_ids_reads = 0

    def get(self, key, default=None):
        if key == "claim_slot_ids":
            self.claim_slot_ids_reads += 1
        return super().get(key, default)


class _CountingRows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _recompute_view_set_hash(view_set):
    view_set["view_set_hash"] = compute_v3_hash({
        key: value for key, value in view_set.items() if key != "view_set_hash"
    })


def test_fact_partition_index_is_equivalent_and_visits_noncritical_rows_once(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=5, slot_count=3,
    )
    view_set = plan["critical_view_set"]
    validated = validate_arbitration_view_set(
        view_set, expected_view_set_hash=view_set["view_set_hash"],
    )
    assert validated == view_set

    counting_rows = _CountingRows(_CountingRow(row) for row in view_set["noncritical_fact_index"])
    counted_view_set = {**view_set, "noncritical_fact_index": counting_rows}
    _validate_fact_partition(counted_view_set, view_set["candidate_hashes"])

    assert counting_rows.iterations == 1
    assert all(row.claim_slot_ids_reads == 1 for row in counting_rows)


def test_fact_partition_index_keeps_rehashed_partition_gap_and_duplicate_gates(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=5, slot_count=3,
    )
    valid_view_set = plan["critical_view_set"]

    partition_gap = copy.deepcopy(valid_view_set)
    assert partition_gap["noncritical_fact_index"]
    partition_gap["noncritical_fact_index"].pop()
    partition_gap["noncritical_fact_count"] = len(partition_gap["noncritical_fact_index"])
    _recompute_view_set_hash(partition_gap)
    with pytest.raises(ArbitrationViewError, match="facts are not a disjoint exhaustive partition"):
        validate_arbitration_view_set(
            partition_gap, expected_view_set_hash=partition_gap["view_set_hash"],
        )

    duplicate_slot = copy.deepcopy(valid_view_set)
    fact = duplicate_slot["noncritical_fact_index"][0]
    fact["claim_slot_ids"].append(fact["claim_slot_ids"][0])
    _recompute_view_set_hash(duplicate_slot)
    with pytest.raises(ArbitrationViewError, match="claim_slot_ids contains duplicate IDs"):
        validate_arbitration_view_set(
            duplicate_slot, expected_view_set_hash=duplicate_slot["view_set_hash"],
        )


def test_fact_partition_index_does_not_stringify_noncritical_candidate_identity():
    candidate_hash = "a" * 64
    fact_identity = {
        "candidate_id": 1,
        "candidate_hash": candidate_hash,
        "fact_kind": "evidence",
        "fact_id": "evidence:one",
        "paper_key": "paper:one",
        "source_fact_hash": "b" * 64,
        "source_inventory_artifact_id": "source-inventory",
        "source_inventory_artifact_hash": "c" * 64,
    }
    view_set = {
        "schema_version": CRITICAL_VIEW_SCHEMA,
        "candidate_ids": ["1"],
        "candidate_hashes": {"1": candidate_hash},
        "critical_views": [],
        "noncritical_fact_index": [{
            **fact_identity,
            "decision_view_id": "decision-view:" + compute_v3_hash(fact_identity)[:24],
            "view_hash": compute_v3_hash(fact_identity),
            "source_binding_hash": "d" * 64,
            "claim_slot_ids": ["slot:one"],
            "claim_group_ids": ["group:one"],
        }],
        "source_slot_scope_index": [{
            "candidate_id": "1",
            "candidate_hash": candidate_hash,
            "claim_slot_id": "slot:one",
            "claim_group_id": "group:one",
            "usage_status": "used",
            "source_claim_ids": [],
            "evidence_ids": ["evidence:one"],
            "source_field_ids": [],
        }],
        "critical_view_count": 0,
        "noncritical_fact_count": 1,
    }
    _recompute_view_set_hash(view_set)

    with pytest.raises(ArbitrationViewError, match="facts are not a disjoint exhaustive partition"):
        validate_arbitration_view_set(
            view_set, expected_view_set_hash=view_set["view_set_hash"],
        )

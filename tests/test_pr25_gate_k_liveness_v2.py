from __future__ import annotations

import json
from pathlib import Path

from runtime.control_plane import ReviewControlPlane


def test_gate_k_liveness_probes_bind_actual_worker_pids_under_python_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Gate K's liveness facts must identify event-producing worker processes."""

    monkeypatch.delenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", raising=False)
    plan_path = tmp_path / "k-acceptance-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "release-acceptance-plan-v2",
                "parent_run_id": "k-locked-launcher-liveness",
                "state_path": "k-acceptance-state.json",
                "budget": {
                    "max_provider_calls_total": 4,
                    "max_output_tokens_total": 1000,
                    "max_retry_attempts_total": 1,
                    "max_wall_seconds": 300,
                },
                "scenarios": {
                    "K": {
                        "scenario_id": "K",
                        "gate": "K",
                        "runtime_spec": "",
                        "workspace": str(tmp_path / "k-workspace"),
                        "input_manifest": "",
                        "execution_mode": "offline-k",
                        "budget_domain": "offline-k",
                        "prerequisites": [],
                        "job_id": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=Path.cwd()).acceptance_run(plan_path)
    assert result["parent_result"]["status"] == "PASS_OFFLINE", json.dumps(
        result, ensure_ascii=False, sort_keys=True
    )
    gate = result["scenarios"]["K"]
    assert gate["status"] == "PASS_OFFLINE"

    evidence_index = json.loads(
        Path(gate["evidence_manifest"]).read_text(encoding="utf-8")
    )
    references = evidence_index["gates"]["K"]["durable_refs"]
    process_events_ref = next(
        item for item in references if item["role"] == "process_events"
    )
    lock_state_ref = next(item for item in references if item["role"] == "lock_state")
    process_events = [
        json.loads(line)
        for line in Path(process_events_ref["path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    lock_state = json.loads(Path(lock_state_ref["path"]).read_text(encoding="utf-8"))

    worker_identities = {
        (
            row["worker_job_id"],
            row["pid"],
            row["process_creation_identity"],
        )
        for row in process_events
        if row.get("event") == "process_started"
    }
    liveness_identities = {
        (
            probe["worker_job_id"],
            probe["target_pid"],
            probe["target_process_creation_identity"],
        )
        for probe in lock_state["liveness_probes"]
        if probe.get("alive") is True
    }

    assert len(worker_identities) == 2
    assert len(liveness_identities) == 2
    assert liveness_identities == worker_identities

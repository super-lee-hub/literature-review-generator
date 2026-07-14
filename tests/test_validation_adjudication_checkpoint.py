import types

from models import APIConfig
from services.job_workspace import JobWorkspace
import validator


def test_adjudication_checkpoint_prevents_repeated_model_call(monkeypatch, tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "project", "job-1")
    generator = types.SimpleNamespace(job_workspace=workspace)
    packet = types.SimpleNamespace(stage="primary")
    packet_dict = {"stage": "primary", "claim": "claim-a"}
    calls = 0

    def fake_stage(_generator, _config, _packet):
        nonlocal calls
        calls += 1
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr(validator, "run_adjudication_stage", fake_stage)
    config: APIConfig = {
        "model": "validator-model",
        "api_key": "secret-a",
        "api_base": "https://validator.example.com/v1",
    }
    first = validator._run_adjudication_stage_checkpointed(
        generator, config, packet, packet_dict, stage="primary"
    )
    rotated_config: APIConfig = {
        "model": "validator-model",
        "api_key": "rotated-secret",
        "api_base": "https://validator.example.com/v1",
    }
    second = validator._run_adjudication_stage_checkpointed(
        generator,
        rotated_config,
        packet,
        packet_dict,
        stage="primary",
    )

    assert first == second
    assert calls == 1

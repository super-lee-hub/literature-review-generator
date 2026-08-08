import multiprocessing
import types

from models import APIConfig
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from validation.adjudication_checkpoint import AdjudicationCheckpointStore, sanitized_route_hash
from validation.execution_service import ValidationExecutionService
from validation.llm_adjudicator import AdjudicationPacket
import validator
import validation.current_validation as current_validation


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


def _multiprocess_checkpoint_call(root_dir, key, entered, release, calls, results):
    store = AdjudicationCheckpointStore(root_dir)
    with store.single_flight(key):
        result = store.load(key)
        if result is None:
            with calls.get_lock():
                calls.value += 1
            entered.set()
            if not release.wait(timeout=20):
                raise TimeoutError("provider release timed out")
            result = {"status": "supported"}
            store.save(key, result)
    results.put(store.load(key))


def test_adjudication_checkpoint_single_flights_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered_first = context.Event()
    entered_second = context.Event()
    release = context.Event()
    calls = context.Value("i", 0)
    results = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_checkpoint_call,
            args=(str(tmp_path), "same-key", entered_first, release, calls, results),
        ),
        context.Process(
            target=_multiprocess_checkpoint_call,
            args=(str(tmp_path), "same-key", entered_second, release, calls, results),
        ),
    ]
    try:
        processes[0].start()
        assert entered_first.wait(timeout=10)
        processes[1].start()
        assert not entered_second.wait(timeout=1)
        assert calls.value == 1
        release.set()
        for process in processes:
            process.join(timeout=15)
            assert not process.is_alive()
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert [results.get(timeout=5), results.get(timeout=5)] == [
        {"status": "supported"},
        {"status": "supported"},
    ]
    assert calls.value == 1


def test_current_adjudication_uses_checkpoint_single_flight(monkeypatch, tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "project", "job-current")
    config = {"api_key": "secret-a", "model": "validator-model", "api_base": "https://example.test"}
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    settings = ApplicationSettings.from_config(
        {
            "Validator_API": dict(config),
            "Runtime": {"max_workers": "1", "validation_retry_limit": "0"},
        }
    )
    service = ValidationExecutionService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        settings=settings,
        summaries=[],
        review_draft_record=None,
        citation_manifest_record=None,
        paper_artifact_records=[],
        visual_artifact_records=[],
        provider_factory=None,
        cancellation_checker=None,
        logger=None,
        runtime_config={"Validator_API": dict(config)},
    )
    result = types.SimpleNamespace(claim_text="claim", paper_ids=["paper-1"], details={})
    packet = AdjudicationPacket(
        citation_set_key="set-1", stage="primary", claim_text="claim", claim_context="",
        block_context="", claim_type="result", claim_type_confidence=1.0,
        claim_type_rationale="", paper_ids=["paper-1"], claim_units=[], target_claim_unit={},
        claim_unit_results=[], paper_identity_hints={}, per_paper_evidence_packets={},
        evidence_excerpt_list=[], trimmed_candidate_counts={}, evidence_status="", disposition="",
    )
    calls = 0
    monkeypatch.setattr(current_validation, "get_validator_api_config", lambda _settings: config)
    monkeypatch.setattr(current_validation, "build_adjudication_packet", lambda *_args, **_kwargs: packet)
    monkeypatch.setattr(current_validation, "_apply_adjudication", lambda item, _report: item)

    def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "supported", "confidence": 0.99}

    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", fake_call)
    assert current_validation._adjudicate(service, [result]) == [result]
    assert current_validation._adjudicate(service, [result]) == [result]
    assert calls == 1


def test_adjudication_key_is_secret_insensitive_and_packet_sensitive(tmp_path):
    store = AdjudicationCheckpointStore(tmp_path)
    route = {"model": "validator", "api_base": "https://example.test", "api_key": "one"}
    rotated = dict(route, api_key="two")
    assert sanitized_route_hash(route) == sanitized_route_hash(rotated)
    first = store.key_for(packet={"claim": "one"}, stage="primary", route_hash=sanitized_route_hash(route))
    second = store.key_for(packet={"claim": "two"}, stage="primary", route_hash=sanitized_route_hash(route))
    assert first != second

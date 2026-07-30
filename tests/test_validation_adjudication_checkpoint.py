import multiprocessing
import threading
import types
from concurrent.futures import ThreadPoolExecutor

from models import APIConfig
from services.job_workspace import JobWorkspace
from validation.adjudication_checkpoint import AdjudicationCheckpointStore
import validator


def _run_checkpointed_paid_call(
    root_dir,
    key,
    ready,
    entered,
    release,
    model_calls,
    results,
):
    store = AdjudicationCheckpointStore(root_dir)
    ready.set()
    try:
        with store.single_flight(key):
            entered.set()
            result = store.load(key)
            if result is None:
                with model_calls.get_lock():
                    model_calls.value += 1
                    call_number = model_calls.value
                if not release.wait(timeout=30):
                    raise TimeoutError("timed out waiting to release paid call")
                result = {"status": "supported", "call_number": call_number}
                store.save(key, result)
        results.put(("ok", store.load(key)))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))
        raise


def _hold_checkpoint_lock(root_dir, key, entered, release):
    store = AdjudicationCheckpointStore(root_dir)
    with store.single_flight(key):
        entered.set()
        release.wait(timeout=30)


def _acquire_checkpoint_lock(root_dir, key, entered):
    store = AdjudicationCheckpointStore(root_dir)
    with store.single_flight(key):
        entered.set()


def _join_process(process, *, timeout=10):
    process.join(timeout=timeout)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise AssertionError(f"child process {process.pid} did not exit")


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


def test_adjudication_checkpoint_single_flights_concurrent_identical_packets(
    monkeypatch, tmp_path
):
    workspace = JobWorkspace.create(str(tmp_path), "project", "job-concurrent")
    generator = types.SimpleNamespace(job_workspace=workspace)
    packet = types.SimpleNamespace(stage="stronger")
    packet_dict = {"stage": "stronger", "claim": "claim-a"}
    config: APIConfig = {
        "model": "validator-model",
        "api_key": "secret-a",
        "api_base": "https://validator.example.com/v1",
    }
    calls = 0
    calls_lock = threading.Lock()
    first_call_started = threading.Event()
    release_first_call = threading.Event()

    def fake_stage(_generator, _config, _packet):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        first_call_started.set()
        assert release_first_call.wait(timeout=5)
        return {"status": "supported", "confidence": 1.0 / call_number}

    monkeypatch.setattr(validator, "run_adjudication_stage", fake_stage)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            validator._run_adjudication_stage_checkpointed,
            generator,
            config,
            packet,
            packet_dict,
            stage="stronger",
        )
        assert first_call_started.wait(timeout=5)
        second = executor.submit(
            validator._run_adjudication_stage_checkpointed,
            generator,
            config,
            packet,
            packet_dict,
            stage="stronger",
        )
        release_first_call.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result == second_result
    assert calls == 1


def test_adjudication_checkpoint_single_flights_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    key = "cross-process-key"
    ready_first = context.Event()
    ready_second = context.Event()
    entered_first = context.Event()
    entered_second = context.Event()
    release = context.Event()
    model_calls = context.Value("i", 0)
    results = context.Queue()
    arguments = (str(tmp_path), key)
    first = context.Process(
        target=_run_checkpointed_paid_call,
        args=(
            *arguments,
            ready_first,
            entered_first,
            release,
            model_calls,
            results,
        ),
    )
    second = context.Process(
        target=_run_checkpointed_paid_call,
        args=(
            *arguments,
            ready_second,
            entered_second,
            release,
            model_calls,
            results,
        ),
    )

    try:
        first.start()
        assert ready_first.wait(timeout=10)
        assert entered_first.wait(timeout=10)
        second.start()
        assert ready_second.wait(timeout=10)
        assert not entered_second.wait(timeout=1)
        assert model_calls.value == 1

        release.set()
        _join_process(first)
        _join_process(second)
    finally:
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    observed = [results.get(timeout=5), results.get(timeout=5)]
    assert observed == [
        ("ok", {"status": "supported", "call_number": 1}),
        ("ok", {"status": "supported", "call_number": 1}),
    ]
    assert model_calls.value == 1


def test_adjudication_checkpoint_process_lock_is_released_after_termination(tmp_path):
    context = multiprocessing.get_context("spawn")
    key = "crash-release-key"
    first_entered = context.Event()
    never_release = context.Event()
    holder = context.Process(
        target=_hold_checkpoint_lock,
        args=(str(tmp_path), key, first_entered, never_release),
    )
    second_entered = context.Event()
    follower = context.Process(
        target=_acquire_checkpoint_lock,
        args=(str(tmp_path), key, second_entered),
    )

    try:
        holder.start()
        assert first_entered.wait(timeout=10)
        holder.terminate()
        holder.join(timeout=10)
        assert not holder.is_alive()

        follower.start()
        assert second_entered.wait(timeout=10)
        _join_process(follower)
    finally:
        for process in (holder, follower):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert holder.exitcode != 0
    assert follower.exitcode == 0

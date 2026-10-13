"""Actual Linux processes and POSIX objects; fake backend, no CARLA claims."""
import multiprocessing as mp
import os
from pathlib import Path
import signal
import sys
import time
import uuid

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/client"))
from easycarla_client import (CarlaEnvClient, ClientBusy, InstanceBusy, ProtocolError,
                              RemoteError, ServiceUnavailable, UnknownOutcomeError, copy_observation)
from easycarla_client.protocol import HEADER_BYTES, decode, encode, read_slot, write_slot
from easycarla_client.transport import Names, cleanup_instance
from easycarla.service import EnvironmentService
from easycarla.service.testing import FakeBackend

CTX = mp.get_context("spawn")


class RejectingResetBackend(FakeBackend):
    """Distinguish preflight rejection from failure after initialization."""
    def reset(self, seed=None, options=None):
        from easycarla.errors import RequestRejected, EnvironmentFault
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise RequestRejected("reset seed must be an integer")
        if options is not None and not isinstance(options, dict):
            raise RequestRejected("reset options must be a mapping")
        if (options or {}).get("reject"):
            raise RequestRejected("requested scene is unavailable")
        result = super().reset(seed, options)
        if (options or {}).get("fail_initialization"):
            raise EnvironmentFault("failure after initialization")
        self.episode_id = getattr(self, "episode_id", 0) + 1
        result[1]["episode_id"] = self.episode_id
        return result

    def experiment(self, method, **kwargs):
        result = super().experiment(method, **kwargs)
        if method == "diagnostics":
            result["episode_id"] = getattr(self, "episode_id", None)
        return result


def run_server(name, config, stop, ready, sizes):
    backend = RejectingResetBackend if (config or {}).get("reject_reset") else FakeBackend
    EnvironmentService(name, lambda: backend(config), **sizes).serve_forever(stop_event=stop, ready_event=ready)


def launch(name, config=None, **sizes):
    stop, ready = CTX.Event(), CTX.Event()
    process = CTX.Process(target=run_server, args=(name, config or {}, stop, ready, sizes))
    process.start()
    assert ready.wait(15), f"server failed to start, exit={process.exitcode}"
    return process, stop


@pytest.fixture
def service():
    name = "test_" + uuid.uuid4().hex
    process, stop = launch(name)
    yield name, process
    stop.set()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    cleanup_instance(name)


def test_codec_nested_arrays_and_scalars():
    value = {"scalar": np.array(4, dtype=np.int64), "matrix": np.arange(20, dtype=np.float32).reshape(5, 4)[:, ::2],
             "$array": "ordinary key", "tuple": (np.array([], dtype=bool), [None, "中文"])}
    payload = encode(value)
    result = decode(payload, len(payload), offset=0)
    assert result["scalar"].shape == ()
    np.testing.assert_array_equal(result["matrix"], value["matrix"])
    assert result["$array"] == "ordinary key"
    assert isinstance(result["tuple"], tuple)
    with pytest.raises(ProtocolError):
        encode(np.array([object()], dtype=object))
    with pytest.raises(ProtocolError):
        encode({"not_finite": float("nan")})


def test_reset_step_close_isolated_and_static_cache(service):
    name, process = service
    with CarlaEnvClient(name) as client:
        obs, info = client.reset(seed=12)
        assert info["backend"] == "fake"
        before = obs["state"].copy()
        time.sleep(0.1)
        assert client.experiment.diagnostics()["frame"] == 0
        new, reward, terminated, truncated, info = client.step([0, .2, 0])
        assert new["frame"] == 4 and reward == pytest.approx(.2)
        assert not terminated and not truncated
        np.testing.assert_array_equal(obs["state"], before)
        first = client.get_static("fake-map-v1")
        seq = client._sequence
        first["points"][:] = -1
        second = client.get_static("fake-map-v1")
        assert client._sequence == seq and second["points"][0, 0] == 0
        assert client.last_timings["transport_and_copy_seconds"] >= 0
    assert process.is_alive()
    with CarlaEnvClient(name) as next_client:
        assert not next_client.experiment.diagnostics()["active"]
        next_client.reset()
        assert next_client.step([0, 0, 1])[0]["frame"] == 4


def test_borrowed_views_and_explicit_copy(service):
    with CarlaEnvClient(service[0], copy=False) as client:
        client.reset()
        obs = client.step([0, .2, 0])[0]
        kept = copy_observation(obs)
        assert not obs["state"].flags.writeable
        with pytest.raises(ValueError):
            obs["state"].setflags(write=True)
        client.step([0, .2, 0])
        assert obs["state"][0] == 8
        assert kept["state"][0] == 4


def test_duplicate_sequence_is_at_most_once(service):
    with CarlaEnvClient(service[0]) as client:
        client.reset()
        result = client.step([0, .2, 0])
        request = client._resources.maps["request"]
        response = client._resources.maps["response"]
        # Repeat the exact published request without invoking the client API.
        client._resources.request_ready.release()
        client._resources.result_ready.acquire(timeout=2)
        _, repeated = read_slot(response)
        assert repeated["value"][4]["total_steps"] == result[4]["total_steps"] == 1
        write_slot(request, epoch=client._epoch, session=client._session,
                   sequence=client._sequence, operation="STEP", value={"action": [0, .3, 0]})
        client._resources.request_ready.release()
        client._resources.result_ready.acquire(timeout=2)
        _, conflict = read_slot(response)
        assert conflict["error"]["code"] == "sequence_conflict"
        assert client.experiment.diagnostics()["total_steps"] == 1


def test_rejected_action_does_not_tick(service):
    with CarlaEnvClient(service[0]) as client:
        client.reset()
        with pytest.raises(RemoteError, match="invalid_request"):
            client.step([0, .2, .2])
        assert client.experiment.diagnostics()["frame"] == 0


def test_instance_and_client_conflicts(service):
    name, _ = service
    with pytest.raises(InstanceBusy):
        EnvironmentService(name, FakeBackend).start()
    with pytest.raises(InstanceBusy):
        cleanup_instance(name)
    with CarlaEnvClient(name):
        with pytest.raises(ClientBusy):
            CarlaEnvClient(name)


def test_capacity_rejection_before_publication(service):
    with CarlaEnvClient(service[0]) as client:
        old_seq = client._sequence
        with pytest.raises(ProtocolError, match="capacity"):
            client.experiment.echo(large=np.zeros(2 << 20, dtype=np.uint8))
        assert client._sequence == old_seq
        assert client.capabilities()["backend"] == "fake"


@pytest.mark.parametrize("iteration", range(6))
def test_timeout_never_replays_and_new_claim_waits(iteration):
    name = "timeout_" + uuid.uuid4().hex
    process, stop = launch(name, {"delay": .35})
    try:
        client = CarlaEnvClient(name, timeout=.12)
        client.reset()
        with pytest.raises(UnknownOutcomeError):
            client.step([0, .2, 0])
        with pytest.raises(UnknownOutcomeError):
            client.step([0, .2, 0])
        client.close()
        with CarlaEnvClient(name, timeout=2) as fresh:
            diagnostics = fresh.experiment.diagnostics()
            assert diagnostics["total_steps"] == 1
            assert not diagnostics["active"]
            fresh.reset()
            assert fresh.step([0, .2, 0])[4]["total_steps"] == 2
    finally:
        stop.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
        cleanup_instance(name)


def dead_client(name, ready):
    client = CarlaEnvClient(name)
    client.reset()
    ready.set()
    time.sleep(60)


def test_client_sigkill_cleans_episode(service):
    ready = CTX.Event()
    process = CTX.Process(target=dead_client, args=(service[0], ready))
    process.start()
    assert ready.wait(10)
    process.kill()
    process.join(5)
    with CarlaEnvClient(service[0]) as client:
        assert not client.experiment.diagnostics()["active"]
        assert client.experiment.diagnostics()["close_count"] == 1


def test_service_sigkill_stale_recovery_and_epoch_change():
    name = "restart_" + uuid.uuid4().hex
    process, stop = launch(name)
    client = CarlaEnvClient(name)
    client.reset()
    old_epoch = client._epoch
    process.kill()
    process.join(5)
    with pytest.raises(ServiceUnavailable):
        client.step([0, 0, 0])
    client._disconnect()
    process2, stop2 = launch(name)
    try:
        with CarlaEnvClient(name) as fresh:
            assert fresh._epoch != old_epoch
            fresh.reset()
            assert fresh.step([0, 0, 0])[0]["frame"] == 4
    finally:
        stop2.set()
        process2.join(5)
        if process2.is_alive():
            process2.kill()
            process2.join()
        cleanup_instance(name)
    import posix_ipc
    with pytest.raises(posix_ipc.ExistentialError):
        posix_ipc.SharedMemory(Names(name).shm("request"))


def test_backend_failure_is_an_error_not_transition():
    name = "failure_" + uuid.uuid4().hex
    process, stop = launch(name, {"fail_step": True})
    try:
        with CarlaEnvClient(name) as client:
            client.reset()
            with pytest.raises(RemoteError, match="backend_error") as err:
                client.step([0, 0, 0])
            assert err.value.details["episode_faulted"]
            assert not client.experiment.diagnostics()["active"]
    finally:
        stop.set()
        process.join(5)
        cleanup_instance(name)


def test_old_session_and_sequence_gap_do_not_execute(service):
    with CarlaEnvClient(service[0]) as client:
        client.reset()
        for session, sequence, expected in [(str(uuid.uuid4()), 2, "stale_session"),
                                            (client._session, 20, "invalid_sequence")]:
            write_slot(client._resources.maps["request"], epoch=client._epoch, session=session,
                       sequence=sequence, operation="STEP", value={"action": [0, .2, 0]})
            client._resources.request_ready.release()
            client._resources.result_ready.acquire(timeout=2)
            _, response = read_slot(client._resources.maps["response"])
            assert response["error"]["code"] == expected
        assert client.experiment.diagnostics()["total_steps"] == 0


def test_result_capacity_error_closes_episode():
    name = "capacity_" + uuid.uuid4().hex
    process, stop = launch(name, response_bytes=2048)
    try:
        with CarlaEnvClient(name) as client:
            client.reset()
            with pytest.raises(RemoteError, match="result_encoding_failed"):
                client.experiment.echo(array=np.zeros(4000, dtype=np.uint8))
            assert not client.experiment.diagnostics()["active"]
    finally:
        stop.set()
        process.join(5)
        cleanup_instance(name)


def test_server_dies_during_action_result_is_unknown():
    import threading
    name = "midstep_" + uuid.uuid4().hex
    process, stop = launch(name, {"delay": 2})
    client = CarlaEnvClient(name, timeout=5)
    try:
        client.reset()
        killer = threading.Timer(.15, process.kill)
        killer.start()
        with pytest.raises(UnknownOutcomeError):
            client.step([0, .2, 0])
        killer.join()
        with pytest.raises(UnknownOutcomeError):
            client.step([0, .2, 0])
        client.close()
    finally:
        client._disconnect()
        if process.is_alive():
            process.kill()
        process.join(5)
        cleanup_instance(name)


def test_forked_child_cannot_release_parent_client_ownership(service):
    with CarlaEnvClient(service[0]) as client:
        child = os.fork()
        if child == 0:
            client._disconnect()
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert status == 0
        with pytest.raises(ClientBusy):
            CarlaEnvClient(service[0])
        assert client.capabilities()["backend"] == "fake"


class DetailedFailure(RuntimeError):
    code = "environment_fault"
    details = {"sensor": "collision", "event_frame": 7, "published_frame": 8,
               "exception": "forged", "episode_faulted": False}


class DetailedFailureBackend(FakeBackend):
    def step(self, action):
        raise DetailedFailure("Late physical frame 7")


def run_detailed_server(name, stop, ready):
    EnvironmentService(name, DetailedFailureBackend).serve_forever(stop_event=stop, ready_event=ready)


def test_backend_fault_context_survives_without_overwriting_server_status():
    name = "detail_" + uuid.uuid4().hex
    stop, ready = CTX.Event(), CTX.Event()
    process = CTX.Process(target=run_detailed_server, args=(name, stop, ready))
    process.start()
    assert ready.wait(15)
    try:
        with CarlaEnvClient(name) as client:
            client.reset()
            with pytest.raises(RemoteError) as caught:
                client.step([0, 0, 0])
            assert caught.value.code == "environment_fault"
            assert caught.value.details["event_frame"] == 7
            assert caught.value.details["published_frame"] == 8
            assert caught.value.details["exception"] == "DetailedFailure"
            assert caught.value.details["episode_faulted"] is True
    finally:
        stop.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
        cleanup_instance(name)


def test_client_claim_waits_for_transient_liveness_probe(service):
    import threading
    from easycarla_client.transport import FileLock
    probe = FileLock(Names(service[0]).client_lock)
    assert probe.acquire()
    release = threading.Timer(.025, probe.release)
    release.start()
    try:
        with CarlaEnvClient(service[0]) as client:
            assert client.capabilities()["backend"] == "fake"
    finally:
        release.join()
        probe.close()


def test_service_start_waits_for_transient_liveness_probe():
    import threading
    from easycarla_client.transport import FileLock
    name = "probe_" + uuid.uuid4().hex
    probe = FileLock(Names(name).server_lock)
    assert probe.acquire()
    release = threading.Timer(.025, probe.release)
    release.start()
    service = EnvironmentService(name, FakeBackend)
    try:
        service.start()
        assert service.backend is not None
    finally:
        release.join()
        service.shutdown()
        probe.close()
        cleanup_instance(name)


class RejectedRequest(ValueError, RuntimeError):
    pass


class RejectingBackend(FakeBackend):
    def experiment(self, method, **kwargs):
        if method == "disabled_operation":
            raise RejectedRequest("This optional operation is disabled")
        return super().experiment(method, **kwargs)


def run_rejecting_server(name, stop, ready):
    EnvironmentService(name, RejectingBackend).serve_forever(stop_event=stop, ready_event=ready)


def test_request_rejected_keeps_episode_alive_through_ipc():
    name = "rejected_" + uuid.uuid4().hex
    stop, ready = CTX.Event(), CTX.Event()
    process = CTX.Process(target=run_rejecting_server, args=(name, stop, ready))
    process.start()
    assert ready.wait(15)
    try:
        with CarlaEnvClient(name) as client:
            client.reset()
            with pytest.raises(RemoteError) as caught:
                client.experiment.disabled_operation()
            assert caught.value.code == "invalid_request"
            state = client.experiment.diagnostics()
            assert state["active"] and state["frame"] == 0
            assert client.step([0, .2, 0])[0]["frame"] == 4
    finally:
        stop.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
        cleanup_instance(name)


class MaterialBackend(FakeBackend):
    """Exercise session-owned material lifetime through the real transport."""
    def __init__(self):
        super().__init__()
        from easycarla.extensions.materials import PrefixMaterialStore
        self.materials = PrefixMaterialStore({"prefix_cache_bytes": 128})

    def experiment(self, method, **kwargs):
        if method == "begin_prefix_import":
            return self.materials.begin_prefix_import(**kwargs)
        if method == "diagnostics":
            return {**super().experiment(method), "material_bytes": self.materials.used_bytes}
        return super().experiment(method, **kwargs)

    def close_client_session(self):
        super().close_client_session()
        self.materials.close()


def run_material_server(name, stop, ready):
    EnvironmentService(name, MaterialBackend).serve_forever(stop_event=stop, ready_event=ready)


def abandon_material_client(name):
    env = CarlaEnvClient(name)
    env.experiment.begin_prefix_import(size_bytes=96, sha256="0" * 64)
    os._exit(0)  # OS releases client flock; no CLOSE request or Python finalizers.


def test_material_cache_clears_on_client_close_and_abandonment_even_without_reset():
    name = "material_" + uuid.uuid4().hex
    stop, ready = CTX.Event(), CTX.Event()
    process = CTX.Process(target=run_material_server, args=(name, stop, ready))
    process.start()
    assert ready.wait(15)
    try:
        with CarlaEnvClient(name) as env:
            env.experiment.begin_prefix_import(size_bytes=64, sha256="0" * 64)
            assert env.experiment.diagnostics()["material_bytes"] == 64
        with CarlaEnvClient(name) as env:
            assert env.experiment.diagnostics()["material_bytes"] == 0
            env.reset(seed=1)
            env.experiment.begin_prefix_import(size_bytes=64, sha256="0" * 64)
            env.reset(seed=2)
            assert env.experiment.diagnostics()["material_bytes"] == 64
        abandoned = CTX.Process(target=abandon_material_client, args=(name,))
        abandoned.start()
        abandoned.join(15)
        assert abandoned.exitcode == 0
        with CarlaEnvClient(name) as env:
            assert env.experiment.diagnostics()["material_bytes"] == 0
            env.experiment.begin_prefix_import(size_bytes=128, sha256="0" * 64)
            assert env.experiment.diagnostics()["material_bytes"] == 128
    finally:
        stop.set()
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert process.exitcode == 0


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf"), True, None, "invalid", 0, -1])
def test_client_rejects_nonfinite_or_invalid_timeouts_before_connecting(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        CarlaEnvClient("not_started", timeout=timeout)


def test_concurrent_close_rejection_preserves_inflight_request_and_connection():
    import threading
    name = "close_busy_" + uuid.uuid4().hex
    process, stop = launch(name, {"delay": .15})
    env = CarlaEnvClient(name)
    results = []
    try:
        env.reset()
        thread = threading.Thread(target=lambda: results.append(env.step([0, .2, 0])))
        thread.start()
        deadline = time.monotonic() + 2
        while not env._mutex.locked() and time.monotonic() < deadline:
            time.sleep(.001)
        with pytest.raises(ClientBusy, match="in flight"):
            env.close()
        assert not env._closed
        assert env._resources is not None
        thread.join(3)
        assert len(results) == 1
        assert int(results[0][0]["frame"]) == 4
        assert int(env.step([0, .2, 0])[0]["frame"]) == 8
    finally:
        env.close()
        stop.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert process.exitcode == 0


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_decoder_rejects_nonfinite_json_constants(token):
    import struct
    metadata = ('{"tree":' + token + ',"arrays":[]}').encode()
    payload = struct.pack("<I", len(metadata)) + metadata
    with pytest.raises(ProtocolError, match="Non-finite JSON"):
        decode(payload, len(payload), offset=0)


def test_notification_exception_after_publication_reports_unknown_and_never_replays():
    name = "notify_failure_" + uuid.uuid4().hex
    process, stop = launch(name)
    env = CarlaEnvClient(name)
    try:
        env.reset()
        original = env._resources.semaphores["request_ready"]
        class InterruptedPublication:
            def release(self):
                original.release()
                raise RuntimeError("interrupted immediately after sem_post")
            def close(self):
                original.close()
        env._resources.semaphores["request_ready"] = InterruptedPublication()
        with pytest.raises(UnknownOutcomeError, match="sem_post"):
            env.step([0, .2, 0])
        assert env._uncertain
        with pytest.raises(UnknownOutcomeError, match="Previous result"):
            env.step([0, .9, 0])
        env.close()
        with CarlaEnvClient(name) as resumed:
            assert resumed.experiment.diagnostics()["total_steps"] in (0, 1)
            assert not resumed.experiment.diagnostics()["active"]
    finally:
        env.close()
        stop.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert process.exitcode == 0

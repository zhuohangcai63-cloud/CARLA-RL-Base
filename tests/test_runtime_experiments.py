"""Experiment dispatch against a fake runtime, never a claimed CARLA replay."""
from collections import deque
from copy import deepcopy
import hashlib
from types import SimpleNamespace as NS

import numpy as np
import pytest

from easycarla.control import ControlArbiter, ReactiveNPCController, TrajectoryTracker
from easycarla.extensions import PrefixCapacityError
from easycarla.extensions.session import ExperimentSession
from easycarla.runtime_experiments import PREFIX_CHUNK_BYTES, capture_initial_manifest, dispatch, raw_state


TOLERANCES = {key: 1e-8 for key in ("position", "velocity", "yaw", "actor_state",
                                   "controllers", "signals", "events", "history", "task")}


def vehicle(identity, x=0):
    return NS(id=identity, type_id="vehicle.fixture", is_alive=True,
              data={"position": [x, 0, 0], "velocity": [0, 0, 0], "yaw": 0,
                    "speed": 0, "acceleration": [0, 0, 0], "control": [0, 0, 0]})


class FakeRuntime:
    def __init__(self):
        self.config = {"scenario": {"source": "mixed", "sources": [{"source": "scenic", "file": "fixture"}]},
                       "simulation": {"physics_dt": 0.05, "decision_dt": 0.2, "history_dt": 0.1},
                       "observations": {"max_objects": 64}, "task": {"profile": "driving"},
                       "experiments": {"enabled": True, "max_prefix_steps": 100}}
        self.resets = 0
        self.experiments = ExperimentSession(self)
        self.reset(15)

    def reset(self, seed=None, options=None):
        self.resets += 1
        self.episode_id = "episode" + str(self.resets)
        self.seed = seed
        self.experiments.on_episode_closed()
        self.elapsed = 0.0
        self.physical_ticks = self.decision_steps = 0
        self.source_config = {"source": "scenic", "file": "fixture"}
        ego, npc = vehicle(self.resets * 10), vehicle(self.resets * 10 + 1, 20)
        registry = NS(roles={ego.id: "ego", npc.id: "npc"}, actors={ego.id: ego, npc.id: npc},
                      physics_enabled={ego.id: True, npc.id: True})
        self.actors = [ego, npc]
        self.session = NS(registry=registry, world=NS(get_actors=lambda: self.actors),
                          controls=NS(export_state=lambda roles: {"schema_version": 1, "owners": {}, "pending": {}}))
        self.snapshot = NS(frame=self.resets * 100, timestamp=NS(elapsed_seconds=self.resets * 100.0))
        npc_ctrl = ReactiveNPCController("npc", [[20, 0], [100, 0]], "C", seed=seed)
        self.episode = NS(ego=ego, controllers={"npc": (npc, npc_ctrl)},
                          metadata={"source": "scenic", "parameters": {"weather": "sun"}},
                          capabilities=lambda: {"prefix_replay": {"available": True, "verified": False, "limitations": []},
                                                "takeover_roles": ["npc"]},
                          info=lambda: {"events": 0}, _tick=0)
        self.observer = NS(history=deque(), route_index=0,
                           semantic_id=lambda actor: 1 if actor.id == ego.id else 2)
        self._sample()
        self.task = NS(previous_progress=0, previous_acceleration=np.zeros(3), events=set())
        self.tracker = TrajectoryTracker()
        self.arbiter = ControlArbiter()
        self.arbiter.claim(ego.id, "client")
        self.arbiter.claim(npc.id, "npc")
        self.last_obs = {"frame": self.snapshot.frame}
        self.episode.export_state = lambda roles: {"schema_version": 1, "scenario_tick": self.episode._tick,
                                                   "summary": self.episode.info(), "limitations": []}
        self.state = "ready"
        self.experiments.on_episode_ready()
        return self.last_obs, {}

    def _sample(self):
        self.observer.history.append((self.snapshot.timestamp.elapsed_seconds,
                                      {self.observer.semantic_id(a): deepcopy(a.data) for a in self.actors}))

    def require_ready(self):
        if self.state != "ready":
            raise RuntimeError("reset required")

    def episode_info(self):
        return {"seed": self.seed}

    def mark_faulted(self):
        self.state = "faulted"

    def rebuild_episode(self, recipe):
        return self.reset(seed=recipe["seed"], options={"recipe": recipe["metadata"]})

    def attach_controller(self, role, lease, controller):
        actor = lease["actor"]
        self.arbiter.claim(actor.id, role)
        self.episode.controllers[role] = (actor, controller)

    def _execute(self, action, command):
        if self.experiments.recorder is not None:
            self.experiments.recorder.ensure_capacity()
        for _ in range(4):
            self.arbiter.begin_tick(self.physical_ticks)
            for _, (actor, controller) in self.episode.controllers.items():
                controller.control(actor.data, [], 0.05, self.physical_ticks)
            self.physical_ticks += 1
        self.episode.ego.data["position"][0] += float(action[1])
        self.episode.ego.data["control"] = list(action)
        self.decision_steps += 1
        self.elapsed += 0.2
        self.episode._tick += 4
        self.snapshot.frame += 4
        self.snapshot.timestamp.elapsed_seconds += 0.2
        self._sample()
        self.last_obs = {"frame": self.snapshot.frame}
        if self.experiments.recorder is not None:
            self.experiments.recorder.append(command, raw_state(self))
        return self.last_obs, 0, False, False, {}

    def step(self, action):
        return self._execute(action, {"method": "step", "action": list(action)})

    def step_trajectory(self, trajectory, execute_seconds):
        assert trajectory["reference_frame"] == self.snapshot.frame
        self.tracker.start(trajectory, self.snapshot.frame, self.episode.ego.data, execute_seconds)
        return self._execute([0, 0.5, 0], {"method": "step_trajectory", "trajectory": deepcopy(trajectory),
                                           "execute_seconds": execute_seconds})


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr("easycarla.extensions.state.actor_state", lambda actor: deepcopy(actor.data))
    return FakeRuntime()


def test_raw_state_normalizes_absolute_frame_time_and_actor_ids(runtime):
    first = raw_state(runtime)
    runtime.reset(15)
    assert first == raw_state(runtime)
    assert first["history"][0]["time"] == 0
    assert set(first["actors"]) == {"ego", "npc"}


def test_prefix_replays_mechanism_commands_and_restores_mixed_source(runtime):
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    dispatch(runtime, "set_mechanism", role="npc", mechanism="L")
    runtime.step([0, 0.4, 0])
    prefix = dispatch(runtime, "finish_prefix", export=True)
    assert [item["command"]["method"] for item in prefix["steps"]] == ["step", "set_mechanism", "step"]
    report = dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert report["report"]["status"] == "passed"
    assert report["report"]["commands_executed"] == 3
    assert runtime.config["scenario"]["source"] == "mixed"
    assert runtime.episode.controllers["npc"][1].mechanism_name == "L"
    assert runtime.experiments.replaying is False


def test_trajectory_reference_is_remapped_after_rebuild(runtime):
    dispatch(runtime, "begin_prefix")
    trajectory = {"reference_frame": runtime.snapshot.frame, "times": [0, 0.2],
                  "points": [[0, 0, 0, 1], [0.2, 0, 0, 1]]}
    runtime.step_trajectory(trajectory, 0.2)
    prefix = dispatch(runtime, "finish_prefix")
    replayed = dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert replayed["report"]["status"] == "passed"
    assert runtime.snapshot.frame != trajectory["reference_frame"] + 4


def test_initial_mechanism_state_is_rebuilt_even_if_changed_before_recording(runtime):
    dispatch(runtime, "set_mechanism", role="npc", mechanism="A")
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    prefix = dispatch(runtime, "finish_prefix")
    replayed = dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert replayed["report"]["status"] == "passed"
    assert runtime.episode.controllers["npc"][1].mechanism_name == "A"


def test_unsupported_native_replay_and_late_prefix_are_rejected_without_reset(runtime):
    runtime.step([0, 0, 0])
    with pytest.raises(ValueError, match="beginning"):
        dispatch(runtime, "begin_prefix")
    runtime.episode.capabilities = lambda: {"prefix_replay": False, "replay_restrictions": ["native state unavailable"]}
    with pytest.raises(RuntimeError, match="native state unavailable"):
        dispatch(runtime, "replay_prefix", prefix={})
    assert runtime.resets == 1


def test_known_capacity_is_checked_before_a_step(runtime):
    runtime.config["experiments"]["max_prefix_steps"] = 1
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.1, 0])
    with pytest.raises(PrefixCapacityError):
        runtime.step([0, 0.9, 0])
    assert runtime.physical_ticks == 4


def test_hidden_state_limitations_prevent_claiming_verified_replay(runtime):
    runtime.episode.capabilities = lambda: {"prefix_replay": {"available": True, "limitations": ["TM state unavailable"]}}
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix")
    # Retain the limitation in reconstructed capabilities, like a real source.
    original_reset = runtime.reset
    def reset(*args, **kwargs):
        result = original_reset(*args, **kwargs)
        runtime.episode.capabilities = lambda: {"prefix_replay": {"available": True, "limitations": ["TM state unavailable"]}}
        return result
    runtime.reset = reset
    report = dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert report["report"]["status"] == "unverified"


def test_takeover_enforces_capabilities_and_installs_exclusive_controller(runtime):
    with pytest.raises(ValueError, match="No takeover"):
        dispatch(runtime, "takeover", role="random_background")
    with pytest.raises(ValueError, match="already managed"):
        dispatch(runtime, "takeover", role="npc")
    actor = vehicle(99, 5)
    runtime.actors.append(actor)
    runtime.session.registry.roles[99] = "adversary"
    runtime.episode.capabilities = lambda: {"takeover_roles": {"adversary": {"can_takeover": True}}, "prefix_replay": False}
    calls = []
    def takeover(role):
        calls.append(role)
        return {"actor": actor, "path": [[5, 0, 0], [50, 0, 0]], "native_lifetime": True}
    runtime.episode.takeover = takeover
    with pytest.raises(ValueError):
        dispatch(runtime, "takeover", role="adversary", mechanism="bad")
    assert calls == []
    response = dispatch(runtime, "takeover", role="adversary", mechanism="G")
    assert response["native_lifetime"]
    assert runtime.arbiter.owner(99) == "adversary"
    assert runtime.episode.controllers["adversary"][1].mechanism_name == "G"


def test_changed_clock_configuration_is_rejected_before_rebuild(runtime):
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix")
    runtime.config["simulation"]["decision_dt"] = 0.4
    with pytest.raises(ValueError, match="same simulation"):
        dispatch(runtime, "replay_prefix", prefix=prefix)
    assert runtime.resets == 1


def test_handle_survives_reset_and_material_roundtrips_in_bounded_chunks(runtime):
    runtime.config["observations"]["fixture_padding"] = "x" * (3 * PREFIX_CHUNK_BYTES)
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.3, 0])
    handle = dispatch(runtime, "finish_prefix")
    assert handle["size_bytes"] > 3 * PREFIX_CHUNK_BYTES
    chunks, offset = [], 0
    while offset < handle["size_bytes"]:
        chunk = dispatch(runtime, "export_prefix_chunk", handle=handle["handle"], offset=offset)
        assert chunk["data"].dtype == np.uint8
        assert len(chunk["data"]) <= PREFIX_CHUNK_BYTES
        chunks.append(chunk["data"].tobytes())
        offset = chunk["next_offset"]
    payload = b"".join(chunks)
    assert hashlib.sha256(payload).hexdigest() == handle["sha256"]
    runtime.reset(15)
    assert dispatch(runtime, "replay_prefix", prefix=handle, tolerances=TOLERANCES)["report"]["status"] == "passed"
    dispatch(runtime, "release_prefix", handle=handle["handle"])
    with pytest.raises(ValueError, match="Unknown prefix"):
        dispatch(runtime, "export_prefix_chunk", handle=handle["handle"])
    imported = dispatch(runtime, "begin_prefix_import", size_bytes=len(payload), sha256=handle["sha256"])
    with pytest.raises(ValueError, match="contiguous"):
        dispatch(runtime, "import_prefix_chunk", handle=imported["handle"], offset=1,
                 data=np.frombuffer(payload[:10], dtype=np.uint8))
    with pytest.raises(ValueError, match="incomplete"):
        dispatch(runtime, "finish_prefix_import", handle=imported["handle"])
    for offset in range(0, len(payload), PREFIX_CHUNK_BYTES):
        dispatch(runtime, "import_prefix_chunk", handle=imported["handle"], offset=offset,
                 data=np.frombuffer(payload[offset:offset + PREFIX_CHUNK_BYTES], dtype=np.uint8))
    imported = dispatch(runtime, "finish_prefix_import", handle=imported["handle"])
    assert dispatch(runtime, "replay_prefix", handle=imported["handle"], tolerances=TOLERANCES)["report"]["status"] == "passed"


def test_prefix_cache_limits_and_bad_import_are_explicit(runtime):
    runtime.config["experiments"]["prefix_cache_bytes"] = 100
    handle = dispatch(runtime, "begin_prefix_import", size_bytes=60, sha256="0" * 64)
    with pytest.raises(RuntimeError, match="cache byte capacity"):
        dispatch(runtime, "begin_prefix_import", size_bytes=60, sha256="0" * 64)
    with pytest.raises(ValueError, match="65536"):
        dispatch(runtime, "import_prefix_chunk", handle=handle["handle"], offset=0,
                 data=np.zeros(PREFIX_CHUNK_BYTES + 1, np.uint8))
    dispatch(runtime, "import_prefix_chunk", handle=handle["handle"], offset=0, data=np.zeros(60, np.uint8))
    with pytest.raises(ValueError, match="checksum"):
        dispatch(runtime, "finish_prefix_import", handle=handle["handle"])
    dispatch(runtime, "release_prefix", handle=handle["handle"])
    assert dispatch(runtime, "begin_prefix_import", size_bytes=60, sha256="0" * 64)["size_bytes"] == 60


def test_initial_manifest_records_physics_and_rejects_changed_reconstruction(runtime):
    for actor in runtime.actors:
        actor.attributes = {"role_name": runtime.session.registry.roles[actor.id]}
        actor.get_physics_control = lambda: NS(mass=1500.0, drag_coefficient=0.3, wheels=[NS(radius=30.0)])
    manifest = capture_initial_manifest(runtime)
    assert [a["spawn_order"] for a in manifest["actors"]] == [0, 1]
    assert manifest["actors"][0]["physics"]["mass"] == 1500
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix")
    with pytest.raises(RuntimeError, match="actor physics"):
        dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert runtime.state == "faulted"


@pytest.mark.parametrize("enabled,active", [(False, True), (None, True), (True, False)])
def test_initial_manifest_never_queries_unsafe_vehicle_physics(runtime, enabled, active):
    actor = runtime.actors[1]
    actor.is_active = active
    if enabled is None:
        runtime.session.registry.physics_enabled.pop(actor.id)
    else:
        runtime.session.registry.physics_enabled[actor.id] = enabled

    def unsafe_getter():
        pytest.fail("unsafe vehicle physics RPC issued")

    actor.get_physics_control = unsafe_getter
    manifest = capture_initial_manifest(runtime)
    assert manifest["actors"][1]["physics"] is None
    assert any("physics.npc" in value for value in manifest["limitations"])
    state = raw_state(runtime)
    assert any("initial_manifest: physics.npc" in value for value in state["limitations"])
    from easycarla.extensions.replay import compare_states
    assert compare_states(state, state, TOLERANCES)["status"] == "unverified"


def test_traffic_rule_state_uses_stable_signal_locations(runtime):
    light = NS(id=98, type_id="traffic.traffic_light", is_alive=True,
               get_transform=lambda: NS(location=NS(x=2, y=3, z=0)),
               get_state=lambda: "Red", is_frozen=lambda: True)
    runtime.actors.append(light)
    runtime.traffic_rules = NS(previous={(98, 1, 2): -2.0}, stopped={(98, 1, 2)})
    runtime.task.events = {"signal:98:1:2"}
    state = raw_state(runtime)
    light.id = 99
    runtime.traffic_rules = NS(previous={(99, 1, 2): -2.0}, stopped={(99, 1, 2)})
    runtime.task.events = {"signal:99:1:2"}
    assert state == raw_state(runtime)


def signed_change(material, change):
    """Re-sign a structurally valid file to exercise semantic preflight checks."""
    from easycarla.extensions.materials import encode_material
    material = deepcopy(material)
    material.pop("sha256")
    change(material)
    material["sha256"] = hashlib.sha256(encode_material(material)).hexdigest()
    return material


@pytest.mark.parametrize("tolerances", [{"position": -1}, {"position": float("nan")},
                                         {"position": True}, {"typo": 0}, []])
def test_bad_tolerances_rejected_without_reset_or_fault(runtime, tolerances):
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix", export=True)
    with pytest.raises(ValueError, match="tolerance"):
        dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=tolerances)
    assert runtime.resets == 1
    assert runtime.state == "ready"
    assert runtime.experiments.replaying is False


@pytest.mark.parametrize("change", [
    lambda record: record["recipe"].update(version=1),
    lambda record: record["recipe"].update(initial_manifest=None),
    lambda record: record["initial_state"].update(schema_version=999),
    lambda record: record["steps"][0]["command"].update(action=[0, 1, 1]),
    lambda record: record["steps"][0].update(command="step"),
    lambda record: record["recipe"]["initial_controllers"]["npc"].update(version="unsupported"),
])
def test_schema_and_commands_preflight_before_reset(runtime, change):
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    prefix = signed_change(dispatch(runtime, "finish_prefix", export=True), change)
    with pytest.raises((ValueError, KeyError)):
        dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert runtime.resets == 1
    assert runtime.state == "ready"
    assert runtime.episode.ego.data["position"][0] == pytest.approx(0.2)


def test_cache_full_finish_can_be_retried_without_losing_recording(runtime):
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    runtime.config["experiments"]["prefix_cache_bytes"] = 1
    with pytest.raises(PrefixCapacityError, match="cache byte"):
        dispatch(runtime, "finish_prefix")
    assert runtime.experiments.recorder.active
    runtime.step([0, 0.3, 0])
    runtime.config["experiments"]["prefix_cache_bytes"] = 64 * 1024 * 1024
    prefix = dispatch(runtime, "finish_prefix")
    result = dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert result["report"]["commands_executed"] == 2


def test_bad_trajectory_is_preflighted_before_any_recorded_command(runtime):
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    prefix = dispatch(runtime, "finish_prefix", export=True)
    def invalid(record):
        record["steps"].append({"command": {"method": "step_trajectory", "execute_seconds": 0.2,
            "trajectory": {"reference_frame": 1, "times": [0, 0], "points": [[0, 0, 0, 1], [1, 0, 0, 1]]}},
            "state": record["initial_state"]})
    with pytest.raises(ValueError, match="times"):
        dispatch(runtime, "replay_prefix", prefix=signed_change(prefix, invalid), tolerances=TOLERANCES)
    assert runtime.resets == 1
    assert runtime.physical_ticks == 4


def test_explicit_event_export_ignores_unrelated_component_attributes(runtime):
    before = raw_state(runtime)
    runtime.episode.events = NS(internal_cache=object(), unrelated_field="not a data schema")
    runtime.unrelated_cache = object()
    assert raw_state(runtime) == before
    runtime.episode.export_state = lambda roles: {"schema_version": 99}
    with pytest.raises(ValueError, match="schema_version"):
        raw_state(runtime)


def test_takeover_failure_after_native_mutation_faults_episode(runtime):
    runtime.episode.capabilities = lambda: {"takeover_roles": {"adversary": {"can_takeover": True}}}
    def failing_takeover(role):
        raise RuntimeError("native controller disabled before error")
    runtime.episode.takeover = failing_takeover
    with pytest.raises(RuntimeError, match="disabled"):
        dispatch(runtime, "takeover", role="adversary")
    assert runtime.state == "faulted"


def test_store_lifecycle_is_explicit_and_no_attributes_are_injected(runtime):
    before = set(vars(runtime))
    dispatch(runtime, "begin_prefix")
    handle = dispatch(runtime, "finish_prefix")
    assert set(vars(runtime)) == before
    runtime.experiments.on_episode_closed()
    assert dispatch(runtime, "export_prefix_chunk", handle=handle["handle"])["size_bytes"] > 0
    runtime.experiments.close()
    assert runtime.experiments.materials.used_bytes == 0
    with pytest.raises(ValueError, match="Unknown prefix"):
        dispatch(runtime, "export_prefix_chunk", handle=handle["handle"])


def test_real_runtime_material_lifetime_distinguishes_episode_and_client_session():
    from easycarla.runtime import EnvironmentRuntime
    runtime = EnvironmentRuntime({"experiments": {"enabled": True}})
    descriptor = runtime.experiments.materials.begin_prefix_import(32, "0" * 64)
    runtime.close_episode()
    assert runtime.experiments.materials.used_bytes == 32
    runtime.close_client_session()
    assert runtime.experiments.materials.used_bytes == 0
    with pytest.raises(ValueError, match="Unknown prefix"):
        runtime.experiments.materials.release_prefix(descriptor["handle"])
    runtime.close_client_session()
    runtime.shutdown()


def test_gateway_pending_control_changes_are_compared(runtime):
    before = raw_state(runtime)
    runtime.session.controls.export_state = lambda roles: {
        "schema_version": 1, "owners": {}, "pending": {"npc": {"set_autopilot": [False, 8000]}}}
    from easycarla.extensions import compare_states
    report = compare_states(before, raw_state(runtime), TOLERANCES)
    assert report["status"] == "failed"
    assert any("control_gateway" in path for path in report["mismatches"])


def test_fault_retains_only_completed_prefix_until_export_or_reset(runtime):
    from easycarla.extensions import validate_prefix
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    runtime.mark_faulted()
    runtime.experiments.on_episode_closed(preserve_recording=True)
    runtime.episode = runtime.observer = None
    assert not runtime.experiments.recorder.active
    prefix = dispatch(runtime, "finish_prefix", export=True)
    material = validate_prefix(prefix)
    assert len(material["steps"]) == 1
    assert prefix["recording_status"] == {"aborted": True, "reason": "episode_faulted",
                                           "includes_faulting_command": False, "commands_recorded": 1}
    runtime.reset(15)
    assert dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)["report"]["status"] == "passed"
    runtime.reset(15)
    dispatch(runtime, "begin_prefix")
    runtime.experiments.on_episode_closed(preserve_recording=True)
    runtime.reset(15)
    with pytest.raises(RuntimeError, match="No prefix"):
        dispatch(runtime, "finish_prefix")


def test_fault_prefix_cache_failure_retains_stopped_material(runtime):
    dispatch(runtime, "begin_prefix")
    runtime.step([0, 0.2, 0])
    runtime.experiments.on_episode_closed(preserve_recording=True)
    runtime.config["experiments"]["prefix_cache_bytes"] = 1
    with pytest.raises(PrefixCapacityError):
        dispatch(runtime, "finish_prefix")
    assert runtime.experiments.recorder is not None
    assert not runtime.experiments.recorder.active
    runtime.config["experiments"]["prefix_cache_bytes"] = 64 * 1024 * 1024
    descriptor = dispatch(runtime, "finish_prefix")
    assert descriptor["recording_status"]["commands_recorded"] == 1
    runtime.experiments.close()
    assert runtime.experiments.materials.used_bytes == 0


@pytest.mark.parametrize("field,value", [("map_layers", "all"), ("no_rendering_mode", True),
                                         ("tile_stream_distance", 500), ("actor_active_distance", 500),
                                         ("reload_on_reset", False), ("spectator_as_ego", True),
                                         ("map_overrides", {"Town13": {"actor_active_distance": 650}})])
def test_world_loading_configuration_is_part_of_recipe_before_reset(runtime, field, value):
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix")
    runtime.config["simulation"][field] = value
    with pytest.raises(ValueError, match="same simulation"):
        dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert runtime.resets == 1


def test_rebuild_rejects_different_effective_settings_even_when_config_inherits_none(runtime):
    assert runtime.config["simulation"].get("tile_stream_distance") is None
    runtime.session.effective_world_settings = {"tile_stream_distance": 500.0,
                                                "actor_active_distance": 500.0}
    capture_initial_manifest(runtime)
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix")
    original_reset = runtime.reset
    def reset(*args, **kwargs):
        result = original_reset(*args, **kwargs)
        runtime.session.effective_world_settings = {"tile_stream_distance": 3000.0,
                                                    "actor_active_distance": 2000.0}
        return result
    runtime.reset = reset
    with pytest.raises(RuntimeError, match="effective world settings"):
        dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert runtime.resets == 2
    assert runtime.physical_ticks == 0
    assert runtime.state == "faulted"


def test_real_session_settings_placeholder_is_not_exported_as_verified_manifest(runtime):
    runtime.session.effective_world_settings = None
    with pytest.raises(RuntimeError, match="Effective world settings"):
        capture_initial_manifest(runtime)


def test_rebuild_rejects_different_applied_map_override(runtime):
    runtime.session.applied_map_override = {"actor_active_distance": 650}
    capture_initial_manifest(runtime)
    dispatch(runtime, "begin_prefix")
    prefix = dispatch(runtime, "finish_prefix")
    original_reset = runtime.reset

    def reset(*args, **kwargs):
        result = original_reset(*args, **kwargs)
        runtime.session.applied_map_override = {}
        return result

    runtime.reset = reset
    with pytest.raises(RuntimeError, match="effective world settings"):
        dispatch(runtime, "replay_prefix", prefix=prefix, tolerances=TOLERANCES)
    assert runtime.resets == 2
    assert runtime.physical_ticks == 0


def test_simulation_recipe_does_not_share_mutable_map_override_values():
    from easycarla.config import validate_config
    from easycarla.extensions.schema import simulation_recipe
    config = validate_config({"simulation": {"map_overrides": {"Town13": {"actor_active_distance": 650}}}})
    recipe = simulation_recipe(config["simulation"])
    config["simulation"]["map_overrides"]["Town13"]["actor_active_distance"] = 700
    assert recipe["map_overrides"]["Town13"]["actor_active_distance"] == 650

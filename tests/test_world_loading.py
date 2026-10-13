"""World-loading configuration contracts without a CARLA process."""
from collections import Counter
from copy import deepcopy
import struct
import threading
from types import SimpleNamespace as NS

import pytest

from easycarla.config import validate_config
from easycarla.errors import EnvironmentFault, WorldInitializationTimeout
from easycarla.simulation.resources import ActorRegistry
from easycarla.simulation.session import WorldSession


class World:
    def __init__(self, streaming=3000, town="Town11"):
        self.town = town
        self.id = 1
        self.callbacks = {}
        self.settings = NS(synchronous_mode=False, fixed_delta_seconds=None,
                           no_rendering_mode=False, substepping=True, max_substeps=10,
                           max_substep_delta_time=.01, tile_stream_distance=streaming,
                           actor_active_distance=min(2000, streaming), spectator_as_ego=True)
        self.applied_settings = []

    def get_map(self):
        return NS(name=f"/Game/Maps/{self.town}/{self.town}")

    def get_settings(self):
        return deepcopy(self.settings)

    def apply_settings(self, settings):
        self.applied_settings.append(deepcopy(settings))
        self.settings = deepcopy(settings)

    def get_snapshot(self):
        return NS(frame=100)

    def set_pedestrians_seed(self, seed):
        pass

    def on_tick(self, callback):
        callback_id = max(self.callbacks, default=0) + 1
        self.callbacks[callback_id] = callback
        return callback_id

    def remove_on_tick(self, callback_id):
        del self.callbacks[callback_id]

    def emit(self, episode_id=None):
        for callback in list(self.callbacks.values()):
            callback(NS(id=self.id if episode_id is None else episode_id, frame=100))


def session(streaming=3000):
    value = WorldSession.__new__(WorldSession)
    value.config = validate_config({"simulation": {"reload_on_reset": False, "map_layers": "none"}})
    value.sim = value.config["simulation"]
    value.registry = ActorRegistry()
    value.controls = NS(clear=lambda: None)
    value.carla = NS(MapLayer=NS(NONE="none", All="all"))
    world, loads = World(streaming), []
    def load(town, **kwargs):
        loads.append((town, kwargs))
        world.id += 1
        world.emit()
        return world
    value.client = NS(get_world=lambda: world, load_world=load,
                      get_available_maps=lambda: [world.get_map().name],
                      get_trafficmanager=lambda port: NS(set_synchronous_mode=lambda value: None,
                                                        set_random_device_seed=lambda seed: None))
    value._original_settings = None
    value._original_map = None
    value._loaded_layers = None
    value.effective_world_settings = {}
    value.ticks, value.timings = Counter(), Counter()
    return value, loads


def test_first_same_town_load_applies_layers_then_reuses_unchanged_world():
    runtime, loads = session()
    runtime.load("Town11", 7)
    assert loads == [("/Game/Maps/Town11/Town11", {"reset_settings": False, "map_layers": "none"})]
    runtime.load("Town11", 8)
    assert len(loads) == 1
    runtime.sim["map_layers"] = "all"
    runtime.load("Town11", 9)
    assert len(loads) == 2 and loads[-1][1]["map_layers"] == "all"
    assert runtime.last_frame == 100 and not runtime.ticks


def test_map_read_waits_for_matching_episode_callback_after_tm_reset(monkeypatch):
    from easycarla.simulation import session as session_module

    runtime, _ = session()
    runtime.sim["timeout"] = 1
    previous = runtime.client.get_world()
    loaded = World(town="Town13")
    loaded.id = previous.id + 1
    loaded.callbacks = previous.callbacks
    waiting, reset_complete, map_read = threading.Event(), threading.Event(), threading.Event()

    class Condition(threading.Condition):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    monkeypatch.setattr(session_module, "threading", NS(Condition=Condition))
    original_get_map = loaded.get_map

    def get_map():
        assert reset_complete.is_set(), "get_map raced the native TM map rebuild"
        map_read.set()
        return original_get_map()

    loaded.get_map = get_map
    runtime.client.get_available_maps = lambda: ["/Game/Maps/Town13/Town13"]

    def load(*args, **kwargs):
        # An old frame, and even a different unexpected episode, must not
        # satisfy the barrier for this particular returned world.
        previous.emit()
        previous.emit(99)
        return loaded

    runtime.client.load_world = load
    errors = []

    def run():
        try:
            runtime.load("Town13", 7)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert waiting.wait(1)
        assert worker.is_alive() and not map_read.is_set()
        # This represents Episode::Listen completing TM Reset before on_tick.
        reset_complete.set()
        loaded.emit()
    finally:
        reset_complete.set()
        loaded.emit()
        worker.join(2)
    assert not worker.is_alive() and not errors
    assert map_read.is_set() and runtime.world is loaded
    assert not previous.callbacks and not runtime.ticks


def test_load_callback_arriving_before_rpc_returns_is_retained():
    runtime, loads = session()
    previous = runtime.client.get_world()
    unrelated = previous.on_tick(lambda snapshot: None)
    runtime.load("Town11", 7)  # fake load delivers before returning
    assert len(loads) == 1
    assert set(previous.callbacks) == {unrelated}, "Only our initialization callback may be removed"


@pytest.mark.parametrize("failure", ["rpc", "timeout", "unchanged_episode"])
def test_load_failure_removes_callback_without_retry_or_extra_tick(failure):
    runtime, _ = session()
    runtime.sim["timeout"] = .005
    previous = runtime.client.get_world()
    loaded = World(town="Town13")
    loaded.id = previous.id + (failure != "unchanged_episode")
    calls = []

    def load(*args, **kwargs):
        calls.append((args, kwargs))
        if failure == "rpc":
            raise RuntimeError("load RPC failed")
        previous.emit()  # never emits the new episode's readiness callback
        return loaded

    runtime.client.load_world = load
    message = {"rpc": "load RPC failed", "timeout": "initialization callback timed out",
               "unchanged_episode": "previous episode"}[failure]
    expected_error = WorldInitializationTimeout if failure == "timeout" else RuntimeError
    with pytest.raises(expected_error, match=message):
        runtime._load_ready_world(previous, "/Game/Maps/Town13/Town13", "all")
    assert len(calls) == 1 and not previous.callbacks
    if failure != "rpc":
        assert runtime.world is loaded, "Partial initialization must retain the touched world for cleanup"


def test_inherited_world_settings_are_recorded_as_effective_values():
    first, _ = session(500)
    second, _ = session(3000)
    first.load("Town11", 7)
    second.load("Town11", 7)
    assert first.sim["tile_stream_distance"] is second.sim["tile_stream_distance"] is None
    assert first.effective_world_settings["tile_stream_distance"] == 500
    assert second.effective_world_settings["tile_stream_distance"] == 3000
    assert first.effective_world_settings != second.effective_world_settings
    assert first.effective_world_settings["map_layers_request"] == "none"


@pytest.mark.parametrize("spectator_as_ego", [False, True])
def test_spectator_streaming_setting_applies_before_and_after_load(spectator_as_ego):
    runtime, _ = session()
    assert runtime.sim["spectator_as_ego"] is False
    runtime.sim["spectator_as_ego"] = spectator_as_ego
    world = runtime.client.get_world()
    original_load = runtime.client.load_world

    def load(*args, **kwargs):
        assert world.settings.spectator_as_ego is spectator_as_ego
        loaded = original_load(*args, **kwargs)
        # Read-back after loading must not depend on settings being inherited.
        loaded.settings.spectator_as_ego = not spectator_as_ego
        return loaded

    runtime.client.load_world = load
    runtime.load("Town11", 7)
    assert [value.spectator_as_ego for value in world.applied_settings] == [spectator_as_ego] * 2
    assert runtime.effective_world_settings["spectator_as_ego"] is spectator_as_ego


def test_spectator_setting_is_restored_to_original_on_session_close(tmp_path):
    runtime, _ = session()
    world = runtime.client.get_world()
    original = world.get_settings()
    runtime._lease = (tmp_path / "world.lock").open("a+")
    runtime.load("Town11", 7)
    assert world.settings.spectator_as_ego is False
    runtime.close()
    assert world.get_settings() == original
    assert runtime._lease.closed


def test_refused_spectator_setting_fails_before_scenario_initialization():
    runtime, _ = session()
    world = runtime.client.get_world()
    original_apply = world.apply_settings

    def apply(settings):
        original_apply(settings)
        world.settings.spectator_as_ego = True

    world.apply_settings = apply
    with pytest.raises(RuntimeError, match="spectator_as_ego"):
        runtime.load("Town11", 7)


def map_loading_client(runtime, initial, loaded_streaming=3000):
    current = [initial]
    calls = []

    def load(asset, **kwargs):
        calls.append(current[0].get_settings())
        previous = current[0]
        current[0] = World(loaded_streaming, asset.rsplit("/", 1)[-1])
        current[0].id = previous.id + 1
        current[0].callbacks = previous.callbacks
        current[0].emit()
        return current[0]

    runtime.client.get_world = lambda: current[0]
    runtime.client.load_world = load
    runtime.client.get_available_maps = lambda: [f"/Game/Maps/{town}/{town}" for town in ("Town11", "Town12", "Town13")]
    return current, calls


def test_map_overrides_do_not_leak_across_a_b_a_or_mutate_base():
    runtime, _ = session()
    runtime.sim.update(tile_stream_distance=3000, actor_active_distance=2000,
                       map_overrides={"Town13": {"tile_stream_distance": 650, "actor_active_distance": 650}})
    original = deepcopy(runtime.sim)
    current, before_load = map_loading_client(runtime, World())
    for town, expected in (("Town13", (650, 650)), ("Town12", (3000, 2000)), ("Town13", (650, 650))):
        runtime.load(town, 7)
        assert (current[0].settings.tile_stream_distance, current[0].settings.actor_active_distance) == expected
        assert (before_load[-1].tile_stream_distance, before_load[-1].actor_active_distance) == expected
        assert runtime.applied_map_override == original["map_overrides"].get(town, {})
        assert runtime.effective_world_settings["map_name"] == town
        assert runtime.sim == original


def test_unspecified_distances_use_new_world_settings_not_old_python_object():
    runtime, _ = session()
    current, _ = map_loading_client(runtime, World(500), loaded_streaming=4200)
    runtime.load("Town13", 7)
    assert runtime.sim["tile_stream_distance"] is None
    assert current[0].settings.tile_stream_distance == 4200
    assert runtime.effective_world_settings["tile_stream_distance"] == 4200
    assert runtime.effective_world_settings["actor_active_distance"] == 2000


def test_partial_override_checks_destination_defaults_after_load():
    runtime, _ = session()
    runtime.sim["map_overrides"] = {"Town13": {"actor_active_distance": 500}}
    current, _ = map_loading_client(runtime, World(100), loaded_streaming=1000)
    runtime.load("Town13", 7)
    assert current[0].settings.actor_active_distance == 500
    assert current[0].settings.tile_stream_distance == 1000


def test_partial_override_rejects_incompatible_destination_defaults():
    runtime, _ = session()
    runtime.sim["map_overrides"] = {"Town13": {"actor_active_distance": 500}}
    map_loading_client(runtime, World(1000), loaded_streaming=100)
    with pytest.raises(ValueError, match="must not exceed"):
        runtime.load("Town13", 7)


@pytest.mark.parametrize("field", ["tile_stream_distance", "actor_active_distance"])
def test_specified_distance_must_match_readback(field):
    runtime, _ = session()
    runtime.sim.update(tile_stream_distance=650, actor_active_distance=650)
    world = runtime.client.get_world()
    apply = world.apply_settings

    def wrong(settings):
        apply(settings)
        setattr(world.settings, field, 640)

    world.apply_settings = wrong
    with pytest.raises(RuntimeError, match=field):
        runtime.load("Town11", 7)


def test_map_override_accepts_exact_asset_path_without_prefix_matching():
    runtime, _ = session()
    runtime.sim.update(tile_stream_distance=3000, actor_active_distance=2000,
                       map_overrides={"Town13": {"tile_stream_distance": 650, "actor_active_distance": 650},
                                      "Town1": {"tile_stream_distance": 100, "actor_active_distance": 100}})
    map_loading_client(runtime, World())
    runtime.load("/Game/Maps/Town13/Town13", 7)
    assert runtime.effective_world_settings["tile_stream_distance"] == 650
    runtime.load("Town11", 7)
    assert runtime.applied_map_override == {}
    assert runtime.effective_world_settings["tile_stream_distance"] == 3000


def test_distance_readback_accepts_native_float32_rounding():
    runtime, _ = session()
    runtime.sim.update(tile_stream_distance=3000.12345, actor_active_distance=650.123)
    world = runtime.client.get_world()
    apply = world.apply_settings

    def float32_settings(settings):
        apply(settings)
        for key in ("tile_stream_distance", "actor_active_distance"):
            value = struct.unpack("f", struct.pack("f", getattr(settings, key)))[0]
            setattr(world.settings, key, value)

    world.apply_settings = float32_settings
    runtime.load("Town11", 7)
    assert runtime.effective_world_settings["tile_stream_distance"] == pytest.approx(3000.12345, rel=1e-6)
    assert runtime.effective_world_settings["actor_active_distance"] == pytest.approx(650.123, rel=1e-6)


@pytest.mark.parametrize("returned_actor", [4000, float("nan"), float("inf")])
def test_malformed_actual_distance_is_rejected_even_when_unspecified(returned_actor):
    runtime, _ = session()
    world = runtime.client.get_world()
    apply = world.apply_settings
    calls = []

    def malformed(settings):
        apply(settings)
        calls.append(None)
        if len(calls) == 2:
            world.settings.actor_active_distance = returned_actor

    world.apply_settings = malformed
    with pytest.raises(RuntimeError, match="actor_active_distance"):
        runtime.load("Town11", 7)


@pytest.mark.parametrize("field,returned", [
    ("synchronous_mode", False), ("no_rendering_mode", True),
    ("substepping", False), ("max_substeps", 9),
    ("max_substep_delta_time", .02), ("max_substep_delta_time", float("nan")),
    ("fixed_delta_seconds", None), ("fixed_delta_seconds", float("nan")),
    ("fixed_delta_seconds", float("inf")), ("fixed_delta_seconds", .06),
])
def test_refused_or_malformed_physics_settings_fail_before_scenario(field, returned):
    runtime, _ = session()
    world = runtime.client.get_world()
    apply = world.apply_settings

    def malformed(settings):
        apply(settings)
        setattr(world.settings, field, returned)

    world.apply_settings = malformed
    with pytest.raises(EnvironmentFault, match=field):
        runtime.load("Town11", 7)


def test_requested_no_rendering_cannot_silently_enable_rendering():
    runtime, _ = session()
    runtime.sim["no_rendering_mode"] = True
    world = runtime.client.get_world()
    apply = world.apply_settings

    def refused(settings):
        apply(settings)
        world.settings.no_rendering_mode = False

    world.apply_settings = refused
    with pytest.raises(EnvironmentFault, match="no_rendering_mode"):
        runtime.load("Town11", 7)

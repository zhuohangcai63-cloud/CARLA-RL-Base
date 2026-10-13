"""One world lease and one tick owner, including initialization and replay."""
from collections import Counter
from copy import deepcopy
import fcntl
import math
import os
from pathlib import Path
import sys
import threading
import time
import numpy as np
from .resources import ActorRegistry
from easycarla.control.gateway import ControlGateway
from easycarla.errors import EnvironmentFault, WorldInitializationTimeout


def resolve_map_asset(client, town):
    """Resolve a name without substituting a different CARLA town or variant."""
    matches = [name for name in client.get_available_maps()
               if name == town or name.rsplit("/", 1)[-1] == town]
    if len(matches) != 1:
        raise ValueError(f"Map {town!r} resolves to {len(matches)} installed assets; supply an exact asset path")
    return matches[0]


def await_loaded_world(client, world, town, timeout):
    """Wait for map metadata visibility after one load RPC; never reload or tick."""
    deadline = time.monotonic() + timeout
    while True:
        current_map = world.get_map()
        if current_map.name.rsplit("/", 1)[-1] == town:
            return world
        if time.monotonic() >= deadline:
            raise EnvironmentFault(f"CARLA loaded {current_map.name!r}, requested {town!r}")
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
        world = client.get_world()


class WorldSession:
    def __init__(self, config):
        self.config = config
        self.sim = config["simulation"]
        api = Path(self.sim["carla_root"]).expanduser().resolve() / "PythonAPI" / "carla"
        if api.is_dir() and str(api) not in sys.path:
            sys.path.insert(0, str(api))
        import carla
        self.carla = carla
        # Keep this identical across independent Conda processes, even when
        # their XDG_RUNTIME_DIR differs.
        lock_dir = Path(f"/tmp/easycarla-{os.getuid()}")
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # V1 reserves CARLA ports per local user, independent of host aliases.
        # localhost/127.0.0.1/the machine hostname must not get separate owners.
        self._lease = open(lock_dir / f"world-port-{self.sim['port']}.lock", "a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease.close()
            raise RuntimeError("This CARLA endpoint already has a tick owner") from None
        try:
            self.client = carla.Client(self.sim["host"], self.sim["port"])
            self.client.set_timeout(self.sim["timeout"])
        except Exception:
            self._lease.close()
            raise
        self.controls = ControlGateway(self)
        self.registry = ActorRegistry(on_forget=self.controls.forget)
        self.world = None
        self.map = None
        self.tm = None
        self._original_settings = None
        self._original_map = None
        self._loaded_layers = None
        self.effective_world_settings = None
        self.applied_map_override = {}
        self.last_frame = None
        self.latest_snapshot = None
        self.world_origin = np.zeros(3, dtype=np.float64)
        self.world_origin_valid = False
        self.ticks = Counter()
        self.timings = Counter()

    @staticmethod
    def _configure_settings(settings, resolved, *, before_load=False):
        settings.synchronous_mode = True
        settings.no_rendering_mode = resolved["no_rendering_mode"]
        settings.spectator_as_ego = resolved["spectator_as_ego"]
        distances = ("tile_stream_distance", "actor_active_distance")
        inherited = {key: getattr(settings, key) for key in distances}
        for key in distances:
            if resolved[key] is not None:
                setattr(settings, key, float(resolved[key]))
        if settings.actor_active_distance > settings.tile_stream_distance:
            if before_load and any(resolved[key] is None for key in distances):
                # A partial override may conflict only with the previous town's
                # inherited values. Validate against the new world after load.
                for key, value in inherited.items():
                    setattr(settings, key, value)
            else:
                raise ValueError("actor_active_distance must not exceed tile_stream_distance")
        settings.fixed_delta_seconds = resolved["physics_dt"]
        settings.substepping = True
        settings.max_substep_delta_time = 0.01
        settings.max_substeps = max(10, int(resolved["physics_dt"] / 0.01 + 1))
        return settings

    @staticmethod
    def _verify_settings(actual, resolved):
        """Reject refused or malformed settings before scenario actors spawn."""
        for key, expected in (
            ("synchronous_mode", True), ("substepping", True),
            ("no_rendering_mode", resolved["no_rendering_mode"]),
            ("spectator_as_ego", resolved["spectator_as_ego"]),
        ):
            value = getattr(actual, key)
            if not isinstance(value, bool) or value != expected:
                raise EnvironmentFault(f"CARLA did not apply {key}")
        for key, expected in (
            ("fixed_delta_seconds", resolved["physics_dt"]),
            ("max_substep_delta_time", 0.01),
        ):
            value = getattr(actual, key)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise EnvironmentFault(f"CARLA returned invalid {key}")
            if not math.isclose(value, expected, rel_tol=1e-6, abs_tol=1e-7):
                raise EnvironmentFault(f"CARLA did not apply {key}")
        expected_substeps = max(10, int(resolved["physics_dt"] / 0.01 + 1))
        if type(actual.max_substeps) is not int or actual.max_substeps != expected_substeps:
            raise EnvironmentFault("CARLA did not apply max_substeps")
        for key in ("tile_stream_distance", "actor_active_distance"):
            value = getattr(actual, key)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise EnvironmentFault(f"CARLA returned invalid {key}")
            if resolved[key] is not None and not math.isclose(value, float(resolved[key]), rel_tol=1e-6, abs_tol=1e-5):
                raise EnvironmentFault(f"CARLA did not apply {key}")
        if actual.actor_active_distance > actual.tile_stream_distance:
            raise EnvironmentFault("CARLA returned actor_active_distance greater than tile_stream_distance")

    def _load_ready_world(self, previous_world, asset, layers):
        """Wait for the loaded episode's callback, without advancing physics.

        CARLA 0.9.15 publishes the new episode ID before resetting Traffic
        Manager. load_world may return at that point; reading get_map can race
        TM's map rebuild. The stream callbacks share one ASIO strand, and
        Episode::Listen invokes on_tick only after TM Reset.
        Register before loading so an early callback cannot be lost.
        """
        previous_id = previous_world.id
        ready_ids = set()
        condition = threading.Condition()

        def on_tick(snapshot):
            with condition:
                ready_ids.add(snapshot.id)
                condition.notify_all()

        callback_id = previous_world.on_tick(on_tick)
        try:
            world = self.client.load_world(asset, reset_settings=False, map_layers=layers)
            self.world = world
            loaded_id = world.id
            if loaded_id == previous_id:
                raise EnvironmentFault("CARLA load_world returned the previous episode")
            with condition:
                if not condition.wait_for(lambda: loaded_id in ready_ids, timeout=self.sim["timeout"]):
                    raise WorldInitializationTimeout(f"CARLA episode {loaded_id} initialization callback timed out")
            return world
        finally:
            # In 0.9.15 World::RemoveOnTick resolves the same Simulator through
            # EpisodeProxy::Lock even after an episode change. This also removes
            # the callback when load_world itself raises or readiness times out.
            previous_world.remove_on_tick(callback_id)

    def load(self, town, seed):
        errors = self.registry.clear()
        if errors:
            raise EnvironmentFault(f"Cannot load a world while owned actors remain: {errors}")
        self.controls.clear()
        world = self.client.get_world()
        # Retain the handle even if later initialization fails, so shutdown can
        # restore the settings of the world we actually touched.
        self.world = world
        if self._original_settings is None:
            self._original_settings = world.get_settings()
            self._original_map = world.get_map().name
        expected_town = town.rsplit("/", 1)[-1]
        override = self.sim.get("map_overrides", {}).get(expected_town, {})
        resolved = {**self.sim, **override}
        self.applied_map_override = deepcopy(override)
        # A real hero owns tile streaming. Without one (loading/cleanup), the
        # spectator should not pull unrelated large-map tiles into memory.
        # Apply before load as well as after it: load_world can tick internally.
        world.apply_settings(self._configure_settings(world.get_settings(), resolved, before_load=True))
        if (world.get_map().name.rsplit("/", 1)[-1] != expected_town or self.sim["reload_on_reset"]
                or self._loaded_layers != self.sim["map_layers"]):
            layers = self.carla.MapLayer.NONE if self.sim["map_layers"] == "none" else self.carla.MapLayer.All
            world = self._load_ready_world(world, resolve_map_asset(self.client, town), layers)
        world = await_loaded_world(self.client, world, expected_town, self.sim["timeout"])
        self.world = world
        # A load can establish different native settings. Do not overwrite
        # unspecified values with a Python object read from the previous map.
        # reset_settings=False can still inherit values on the server: specify
        # base distances when cross-map restoration must be unambiguous.
        world.apply_settings(self._configure_settings(world.get_settings(), resolved))
        actual = world.get_settings()
        self._verify_settings(actual, resolved)
        self._loaded_layers = self.sim["map_layers"]
        self.effective_world_settings = {key: getattr(actual, key) for key in (
            "synchronous_mode", "fixed_delta_seconds", "no_rendering_mode", "substepping",
            "max_substeps", "max_substep_delta_time", "tile_stream_distance", "actor_active_distance",
            "spectator_as_ego")}
        # CARLA exposes no query for the currently loaded layer bitmask. Preserve
        # the explicit request separately from read-back WorldSettings values.
        self.effective_world_settings["map_layers_request"] = self._loaded_layers
        self.effective_world_settings["map_name"] = expected_town
        self.world = world
        self.map = world.get_map()
        self.tm = self.client.get_trafficmanager(self.sim["tm_port"])
        self.tm.set_synchronous_mode(True)
        self.tm.set_random_device_seed(int(seed) % (2**31))
        world.set_pedestrians_seed(int(seed) % (2**31))
        self.last_frame = world.get_snapshot().frame
        self.world_origin = np.zeros(3, dtype=np.float64)
        self.world_origin_valid = False
        self.ticks.clear()
        self.timings.clear()

    def tick(self, phase="step"):
        if self.world.get_snapshot().frame != self.last_frame:
            raise RuntimeError("Unexpected world frame advance: another client is ticking")
        before_control = time.perf_counter()
        self.controls.flush()
        self.timings["control_commit_seconds"] += time.perf_counter() - before_control
        start = time.perf_counter()
        frame = self.world.tick(self.sim["timeout"])
        if frame != self.last_frame + 1:
            raise RuntimeError(f"Non-contiguous physical frame {self.last_frame} -> {frame}")
        self.last_frame = frame
        snapshot = self.world.get_snapshot()
        if snapshot.frame != frame:
            raise RuntimeError("World advanced during snapshot read")
        self.latest_snapshot = snapshot
        self.ticks[phase] += 1
        self.controls.after_tick()
        self.timings["simulation_seconds"] += time.perf_counter() - start
        return snapshot

    def close(self):
        if self._lease.closed:
            return
        errors = self.registry.clear()
        try:
            for cleanup in (
                lambda: self.tm.set_synchronous_mode(False) if self.tm is not None else None,
                lambda: self.world.apply_settings(self._original_settings)
                if self.world is not None and self._original_settings is not None else None,
            ):
                try:
                    cleanup()
                except Exception as exc:
                    errors.append(str(exc))
        finally:
            fcntl.flock(self._lease, fcntl.LOCK_UN)
            self._lease.close()
            self.controls.clear()
        if errors:
            raise EnvironmentFault(f"World shutdown cleanup failed: {errors}")

"""Frame-addressed sensors and explicit sparse-event lateness detection.

A CPU IMU provides a heartbeat every physics frame. Sparse collision streams do
not provide a zero-events completion message: a finite settle window is a bounded
latency allowance, not proof that all events arrived. A late event faults the
transition instead of silently assigning a past collision to a later frame.
"""
from collections import defaultdict
from threading import Condition
import math
import time
import numpy as np
from easycarla.observations.geometry import pose


class SensorTimeout(RuntimeError):
    pass


class LateSensorEvent(RuntimeError):
    code = "late_sensor_event"

    def __init__(self, details):
        self.details = dict(details)
        super().__init__(f"Late collision event at physical frame {details['event_frame']} "
                         f"after sealing frame {details['sealed_frame']}; "
                         f"last published frame {details['published_frame']}. "
                         "The affected transition is invalid; do not shift its reward to a later frame.")


class SensorSynchronizer:
    def __init__(self, names, timeout=10.0, generation=0):
        self.names = tuple(names)
        self.timeout = timeout
        self.generation = generation
        self.frames = defaultdict(dict)
        self.condition = Condition()
        self.closed = False

    def push(self, name, data, generation=None):
        with self.condition:
            if self.closed or (generation is not None and generation != self.generation):
                return
            if name not in self.names:
                return
            self.frames[int(data.frame)][name] = data
            while len(self.frames) > 64:
                del self.frames[min(self.frames)]
            self.condition.notify_all()

    def collect(self, frame):
        deadline = time.monotonic() + self.timeout
        with self.condition:
            while not all(name in self.frames.get(frame, {}) for name in self.names):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.closed:
                    missing = set(self.names) - self.frames.get(frame, {}).keys()
                    raise SensorTimeout(f"Missing sensors {sorted(missing)} at frame {frame}")
                self.condition.wait(remaining)
            if self.closed:
                raise SensorTimeout("Sensor synchronizer closed")
            result = self.frames.pop(frame, {})
            for old in [f for f in self.frames if f < frame]:
                del self.frames[old]
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.frames.clear()
            self.condition.notify_all()


class CollisionBarrier:
    """Lock sparse events against frame sealing and transition publication."""
    def __init__(self, generation, settle_seconds=0.002):
        if not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 0.1:
            raise ValueError("collision_settle_seconds must be finite and between 0 and 0.1")
        self.generation = generation
        self.settle_seconds = float(settle_seconds)
        self.condition = Condition()
        self.events = []
        self.late = None
        self.sealed_frame = -1
        self.published_frame = -1
        self.closed = False

    def _check(self):
        if self.late is not None:
            raise LateSensorEvent(self.late)
        if self.closed:
            raise SensorTimeout("Collision barrier closed")

    def push(self, frame, actor_id, generation):
        with self.condition:
            if self.closed or generation != self.generation:
                return
            frame, actor_id = int(frame), int(actor_id)
            if frame <= self.sealed_frame:
                if self.late is None:
                    self.late = {"sensor": "collision", "event_frame": frame,
                                 "sealed_frame": self.sealed_frame,
                                 "published_frame": self.published_frame, "actor_id": actor_id}
            else:
                self.events.append((frame, actor_id))
            self.condition.notify_all()

    def assert_healthy(self):
        with self.condition:
            self._check()

    def collect(self, frame):
        """Seal one completed physical frame after a fixed wall-clock allowance."""
        with self.condition:
            self._check()
            if frame <= self.sealed_frame:
                raise RuntimeError("Physical sensor frames must be collected exactly once in increasing order")
            deadline = time.monotonic() + self.settle_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
                self._check()
            self._check()
            self.sealed_frame = int(frame)

    def commit(self, frame):
        """Runtime calls immediately before returning reset/step's observation."""
        with self.condition:
            self._check()
            if frame != self.sealed_frame or frame < self.published_frame:
                raise RuntimeError("Only the most recently collected sensor frame can be published")
            self.published_frame = int(frame)

    def snapshot(self):
        with self.condition:
            self._check()
            return list(self.events)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class SensorSuite:
    HEARTBEAT = "__frame_heartbeat"

    def __init__(self, session, ego, config, generation):
        self.session, self.ego, self.config = session, ego, config
        self.actors = []
        self.generation = generation
        self.active = True
        self.collision_barrier = CollisionBarrier(generation, config.get("collision_settle_seconds", 0.002))
        names = [self.HEARTBEAT] + [n for n in ("rgb", "bev", "lidar") if config[n]]
        self.sync = SensorSynchronizer(names, config["sensor_timeout"], generation)
        try:
            self._spawn()
        except Exception:
            self.close()
            raise

    def _spawn(self):
        session, ego, config, generation = self.session, self.ego, self.config, self.generation
        c = session.carla
        lib = session.world.get_blueprint_library()
        collision = session.world.spawn_actor(lib.find("sensor.other.collision"), c.Transform(), attach_to=ego)
        self._register(collision, "collision")
        collision.listen(lambda event: self._collision(event, generation))
        for name in self.sync.names:
            sensor_id = {self.HEARTBEAT: "sensor.other.imu", "rgb": "sensor.camera.rgb",
                         "bev": "sensor.camera.semantic_segmentation", "lidar": "sensor.lidar.ray_cast"}[name]
            bp = lib.find(sensor_id)
            # Zero means every physics frame; decimal intervals can round above
            # fixed_delta_seconds and accidentally skip alternating frames.
            bp.set_attribute("sensor_tick", "0.0")
            if name == self.HEARTBEAT:
                transform = c.Transform()
            elif name == "lidar":
                bp.set_attribute("channels", "32")
                bp.set_attribute("range", str(config["range"]))
                bp.set_attribute("points_per_second", "100000")
                bp.set_attribute("rotation_frequency", str(1 / session.sim["physics_dt"]))
                transform = c.Transform(c.Location(z=2.4))
            elif name == "bev":
                bp.set_attribute("image_size_x", str(config["bev_size"]))
                bp.set_attribute("image_size_y", str(config["bev_size"]))
                bp.set_attribute("fov", "90")
                transform = c.Transform(c.Location(z=40), c.Rotation(pitch=-90))
            else:
                bp.set_attribute("image_size_x", str(config["image_width"]))
                bp.set_attribute("image_size_y", str(config["image_height"]))
                transform = c.Transform(c.Location(x=1.5, z=2.0))
            actor = session.world.spawn_actor(bp, transform, attach_to=ego)
            self._register(actor, name)
            actor.listen(lambda data, name=name: self.sync.push(name, data, generation))

    def _register(self, actor, name):
        self.session.registry.register(actor, f"sensor/{name}")
        self.actors.append(actor)

    @property
    def collisions(self):
        return self.collision_barrier.snapshot()

    def _collision(self, event, generation=None):
        self.collision_barrier.push(event.frame, event.other_actor.id,
                                    self.generation if generation is None else generation)

    def assert_healthy(self):
        self.collision_barrier.assert_healthy()

    def commit(self, frame):
        self.collision_barrier.commit(frame)

    def collect(self, frame):
        self.assert_healthy()
        data = self.sync.collect(frame)
        self.collision_barrier.collect(frame)
        heartbeat = data.get(self.HEARTBEAT)
        snapshot = getattr(getattr(self, "session", None), "latest_snapshot", None)
        if snapshot is not None and getattr(heartbeat, "transform", None) is not None:
            from easycarla.observations.vehicle_geometry import world_origin_from_heartbeat
            origin = world_origin_from_heartbeat(snapshot, self.ego.id, heartbeat)
            self.session.world_origin = origin
            self.session.world_origin_valid = True
        result = {}
        # The world remains paused at this frame. Packet transforms contain the
        # full sensor pose (including bank/slope), while policy geometry uses a
        # yaw-only, world-up ego frame.
        if not any(name != self.HEARTBEAT for name in data):
            return result
        origin, yaw = pose(self.ego.get_transform())
        c, s = math.cos(yaw), math.sin(yaw)
        world_to_ego = np.eye(4, dtype=np.float64)
        world_to_ego[:3, :3] = [[c, s, 0], [-s, c, 0], [0, 0, 1]]
        world_to_ego[:3, 3] = -world_to_ego[:3, :3] @ origin
        handedness = np.diag([1.0, -1.0, 1.0, 1.0])
        for name, packet in data.items():
            if name == self.HEARTBEAT:
                continue
            # The matrices map a sensor-local RH FLU point, not an image pixel,
            # into world RH or levelled ego FLU. Both are per-packet extrinsics.
            sensor_to_world = handedness @ np.asarray(packet.transform.get_matrix(), np.float64) @ handedness
            if getattr(getattr(self, "session", None), "world_origin_valid", False):
                sensor_to_world[:3, 3] += self.session.world_origin
            sensor_to_ego = world_to_ego @ sensor_to_world
            extrinsics = {"sensor_frame": "sensor_flu", "sensor_to_world": sensor_to_world,
                          "sensor_to_ego": sensor_to_ego}
            if name == "lidar":
                points = np.frombuffer(packet.raw_data, dtype=np.float32).reshape(-1, 4).copy()
                points[:, 1] *= -1
                points[:, :3] = points[:, :3] @ sensor_to_ego[:3, :3].T + sensor_to_ego[:3, 3]
                limit = self.config["lidar_points"]
                result[name] = {"points": points[:limit], "count": min(len(points), limit),
                                "truncated": max(0, len(points) - limit), "frame": frame,
                                "timestamp": packet.timestamp, "coordinate_frame": "ego_flu", **extrinsics}
            else:
                bgra = np.frombuffer(packet.raw_data, dtype=np.uint8).reshape(packet.height, packet.width, 4)
                pixels = bgra[:, :, 2].copy() if name == "bev" else bgra[:, :, 2::-1].copy()
                result[name] = {"data": pixels, "frame": frame, "timestamp": packet.timestamp,
                                "coordinate_frame": "image_uv", **extrinsics}
        return result

    def close(self):
        self.active = False
        self.collision_barrier.close()
        self.sync.close()
        self.session.registry.destroy([a.id for a in self.actors])

"""Narrow, owned context for migrated Scenic event implementations."""
from collections import Counter
import numpy as np
from easycarla.errors import EnvironmentFault


class OwnedWorld:
    def __init__(self, session):
        self.session = session
        self.counters = Counter()

    def __getattr__(self, name):
        if name in ("tick", "wait_for_tick"):
            raise RuntimeError("Scenario code cannot advance the world")
        return getattr(self.session.world, name)

    def _spawn(self, method, blueprint, transform, *args, semantic_role=None, **kwargs):
        actor = getattr(self.session.world, method)(blueprint, transform, *args, **kwargs)
        if actor is not None:
            # CARLA role_name has engine semantics (e.g. hero tile streaming).
            # A stable research identity can be declared independently.
            role = semantic_role
            if role is None:
                role = blueprint.get_attribute("role_name") if blueprint.has_attribute("role_name") else blueprint.id
            role = str(role)
            self.counters[role] += 1
            self.session.registry.register(actor, f"scenic/{role}/{self.counters[role]}")
        return actor

    def spawn_actor(self, blueprint, transform, *args, **kwargs):
        return self._spawn("spawn_actor", blueprint, transform, *args, **kwargs)

    def try_spawn_actor(self, blueprint, transform, *args, **kwargs):
        return self._spawn("try_spawn_actor", blueprint, transform, *args, **kwargs)


class EventContext:
    def __init__(self, session, ego, route):
        self.session, self.ego, self.global_route = session, ego, route
        self.world = OwnedWorld(session)
        self.town_name = session.map.name.rsplit("/", 1)[-1]
        self.dt = session.sim["physics_dt"]
        self.tm_port = session.sim["tm_port"]
        self.time_step = 0
        self.npc_vehicle_blacklist = set()

    @staticmethod
    def _control_source(source):
        return "scenic/event/" + source

    def control(self, actor, control, *, source):
        self.session.controls.apply_control(actor, control, owner=self._control_source(source))

    def set_autopilot(self, actor, enabled, *, source):
        owner = self._control_source(source)
        current = self.session.controls.owner(actor.id)
        if not enabled and current not in (None, owner):
            self.session.controls.handoff(actor, owner, expected_owner=current, mode="manual")
        self.session.controls.set_autopilot(actor, enabled, owner=owner, port=self.tm_port)

    def set_target_velocity(self, actor, velocity, *, source):
        self.session.controls.set_target_velocity(actor, velocity, owner=self._control_source(source))

    def _destroy_actor_ids_batch(self, ids, label="actors", do_tick=False):
        if do_tick:
            raise RuntimeError("Event cleanup must not tick")
        failures = self.session.registry.destroy(ids)
        if failures:
            raise EnvironmentFault(f"Event cleanup failed for {label}: {failures}")
        return failures

    @staticmethod
    def _advance_waypoint_along_lane(waypoint, distance, step_size=2.0):
        travelled = 0.0
        while travelled < distance:
            step = min(step_size, distance - travelled)
            choices = waypoint.next(step)
            if not choices:
                return None
            waypoint = min(choices, key=lambda w: (w.road_id, w.lane_id, w.s))
            travelled += step
        return waypoint

    def _world_to_ego_local(self, ego_transform, location):
        ego_loc = ego_transform.location
        ego_yaw = np.deg2rad(ego_transform.rotation.yaw)
        rel_x = location.x - ego_loc.x
        rel_y = location.y - ego_loc.y
        local_x = np.cos(-ego_yaw) * rel_x - np.sin(-ego_yaw) * rel_y
        local_y = np.sin(-ego_yaw) * rel_x + np.cos(-ego_yaw) * rel_y
        return local_x, local_y


    @staticmethod
    def _actor_speed(actor):
        velocity = actor.get_velocity()
        return float(np.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2))


    @staticmethod
    def _normalize_xy(vec_x, vec_y):
        norm = float(np.sqrt(vec_x ** 2 + vec_y ** 2))
        if norm < 1e-6:
            return None
        return np.array([vec_x / norm, vec_y / norm], dtype=np.float32)


    @staticmethod
    def _yaw_diff_deg(yaw_a, yaw_b):
        return abs((yaw_a - yaw_b + 180.0) % 360.0 - 180.0)

"""State history with stable IDs, masks, current-pose compensation and no labels."""
from collections import deque
import hashlib
import time
import numpy as np
from .geometry import xyz, pose, local, wrap
from .maps import build_map


from .frame import actor_state, capture_frame, FrozenMapping, FrameStateError


class ObservationBuilder:
    def __init__(self, session, episode, config):
        self.session, self.episode = session, episode
        self.config = config["observations"]
        self.history_dt = config["simulation"]["history_dt"]
        self.planning = self.config.get("profile") == "planning_v1"
        self.geometry_cache = {} if self.planning else None
        self.signal_cache = {} if config["performance"]["deferred_observations"] else None
        self.history_interval = int(round(config["simulation"]["history_dt"] / config["simulation"]["physics_dt"]))
        self.history = deque(maxlen=self.config["history_frames"])
        if self.planning:
            from .routes import build_planning_map
            self.static = build_planning_map(session.map)
        else:
            self.static = build_map(session.map)
        self.semantic = {}
        self.next_id = 1
        self.last_sample = None
        self.route = np.asarray([xyz(t.location) for t in episode.route], np.float64).reshape(-1, 3)
        self.route_s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(self.route, axis=0), axis=1))]
        self.route_index = 0
        self.origin_frame = None
        self.last_states = FrozenMapping({})
        self.frame_state = None
        self.route_segment_index = 0
        self.route_description = None
        self.map_obstacles = []
        self.last_sample_timings = {}
        if self.planning:
            self.static = dict(self.static)

    def _planning_content(self):
        if self.route_description is None:
            from .planning import map_obstacles
            from .routes import describe_route
            self.map_obstacles = map_obstacles(self.session)
            self.static = dict(self.static)
            self.static["static_objects"] = self.map_obstacles
            import json
            props = json.dumps(self.map_obstacles, sort_keys=True).encode()
            self.static["map_id"] = hashlib.sha256(self.static["map_id"].encode() + props).hexdigest()
            self.static["object_geometry_source"] = "CARLA_environment_object_world_bounding_box"
            self.static["moving_geometry_source"] = "CARLA_bbox_and_0.9.15_physics_wheels"
            self.static["point_kinematics_source"] = "CARLA_actor_snapshot_with_planar_rigid_body_offset_correction"
            self.static["origin_source"] = "identity_attached_IMU_header_vs_same_frame_ActorSnapshot"
            self.route_description = describe_route(self.session, self.episode.route, self.static)

    def semantic_id(self, actor):
        return self.semantic_id_from_id(actor.id)

    def semantic_id_from_id(self, actor_id):
        if actor_id not in self.semantic:
            role = self.session.registry.roles.get(actor_id)
            if role:
                value = int.from_bytes(hashlib.blake2b(role.encode(), digest_size=8).digest(), "little") & ((1 << 63) - 1)
            else:
                value = self.next_id
                self.next_id += 1
            self.semantic[actor_id] = value
        return self.semantic[actor_id]

    def get_state(self, actor_or_id, *, frame=None):
        """Return immutable state from the latest captured frame, without CARLA RPCs."""
        if self.frame_state is None:
            raise FrameStateError("No physical frame has been captured")
        if frame is not None and int(frame) != self.frame_state.frame:
            raise FrameStateError(f"Requested frame {frame}, captured frame is {self.frame_state.frame}")
        actor_id = actor_or_id.id if hasattr(actor_or_id, "id") else int(actor_or_id)
        try:
            return self.frame_state.actors[actor_id]
        except KeyError as exc:
            raise FrameStateError(f"Actor {actor_id} is absent from frame {self.frame_state.frame}") from exc

    def sample(self, snapshot, tick):
        if self.frame_state is not None:
            if snapshot.frame < self.frame_state.frame:
                raise FrameStateError("Physical frames cannot move backward within an episode")
            if snapshot.frame == self.frame_state.frame:
                if float(snapshot.timestamp.elapsed_seconds) != self.frame_state.timestamp:
                    raise FrameStateError("One physical frame cannot have two timestamps")
                return self.last_states
        if self.origin_frame is None:
            self.origin_frame = snapshot.frame
        if self.planning:
            self._planning_content()
        capture_start = time.perf_counter()
        captured = capture_frame(self.session, snapshot, self.geometry_cache, self.signal_cache, self.episode.ego.id)
        if self.episode.ego.id not in captured.actors:
            raise FrameStateError(f"Ego actor is absent from physical frame {snapshot.frame}")
        self.frame_state = captured
        if self.planning:
            # Derive planar rear-axle acceleration from the committed angular
            # rates. The first sample explicitly has no angular acceleration.
            previous = self.last_states
            enriched = {}
            for aid, value in captured.actors.items():
                row = dict(value)
                sid = self.semantic_id_from_id(aid)
                old = previous.get(sid)
                dt = captured.timestamp - getattr(self, "previous_timestamp", captured.timestamp)
                valid = old is not None and dt > 0 and row["geometry"]["rear_valid"]
                offset = np.asarray(row["rear_offset_world"])
                omega = row["yaw_rate"]
                alpha = (omega-old["yaw_rate"])/dt if old is not None and dt > 0 else 0.
                row["rear_acceleration"] = (np.asarray(row["acceleration"]) +
                    np.array([-alpha*offset[1]-omega**2*offset[0], alpha*offset[0]-omega**2*offset[1], 0])).tolist()
                row["rear_acceleration_valid"] = valid
                bbox = np.asarray(row["bbox_offset_world"])
                row["bbox_acceleration"] = (np.asarray(row["acceleration"]) +
                    np.array([-alpha*bbox[1]-omega**2*bbox[0], alpha*bbox[0]-omega**2*bbox[1], 0])).tolist()
                row["bbox_acceleration_valid"] = old is not None and dt > 0 and row["geometry"]["bbox_valid"]
                enriched[aid] = row
            from dataclasses import replace
            captured = replace(captured, actors=FrozenMapping(enriched))
            self.frame_state = captured
            self.previous_timestamp = captured.timestamp
        capture_seconds = time.perf_counter() - capture_start
        history_start = time.perf_counter()
        states = {}
        for actor_id, state in captured.actors.items():
            sid = self.semantic_id_from_id(actor_id)
            states[sid] = state
        self.last_states = FrozenMapping(states)
        if self.last_sample is None or tick % self.history_interval == 0:
            self.history.append((captured.timestamp, self.last_states))
            self.last_sample = tick
        self.last_sample_timings = {"frame_capture_seconds": capture_seconds,
                                    "history_sampling_seconds": time.perf_counter() - history_start}
        return self.last_states

    def build(self, snapshot, sensors=None):
        ego = self.get_state(self.episode.ego, frame=snapshot.frame)
        if float(snapshot.timestamp.elapsed_seconds) != self.frame_state.timestamp:
            raise FrameStateError("Observation timestamp differs from the captured physical frame")
        origin, yaw = np.asarray(ego["position"]), ego["yaw"]
        ego_id = self.semantic_id(self.episode.ego)
        candidates = [(np.linalg.norm(np.asarray(s["position"]) - origin), sid)
                      for sid, s in self.last_states.items() if sid != ego_id]
        candidates = sorted((d, sid) for d, sid in candidates if d <= self.config["range"])
        chosen = [sid for _, sid in candidates[:self.config["max_objects"]]]
        n, h = self.config["max_objects"], self.config["history_frames"]
        objects = {k: np.zeros((h, n, 3), np.float32) for k in ("position", "velocity", "acceleration", "size")}
        objects.update({"yaw": np.zeros((h, n), np.float32), "yaw_rate": np.zeros((h, n), np.float32),
                        "valid": np.zeros((h, n), bool), "type": np.zeros((h, n), np.uint8),
                        "ids": np.zeros(n, np.int64), "timestamps": np.zeros(h, np.float64)})
        objects["ids"][:len(chosen)] = chosen
        for hi, (ts, frame_states) in enumerate(self.history, start=h - len(self.history)):
            objects["timestamps"][hi] = ts
            for j, sid in enumerate(chosen):
                state = frame_states.get(sid)
                if state is None:
                    continue
                for key in ("position", "velocity", "acceleration"):
                    objects[key][hi, j] = local(state[key], origin, yaw, vector=key != "position")
                objects["size"][hi, j] = state["size"]
                objects["yaw"][hi, j] = wrap(state["yaw"] - yaw)
                objects["yaw_rate"][hi, j] = state["yaw_rate"]
                objects["valid"][hi, j] = True
                objects["type"][hi, j] = 2 if state["type"].startswith("walker.") else 1
        objects["count"] = len(chosen)
        objects["truncated"] = max(0, len(candidates) - n)
        self.update_route(origin)
        count = self.config["route_points"]
        route = np.zeros((count, 3), np.float32)
        segment = self.route[self.route_index:self.route_index + count]
        route[:len(segment)] = local(segment, origin, yaw)
        route_valid = np.arange(count) < len(segment)
        ego_obs = self.task_state()["ego"]
        ego_obs.update(position_world=origin.astype(np.float64), yaw_world=float(yaw),
                       velocity=local(ego["velocity"], origin, yaw, vector=True),
                       yaw_rate=float(ego["yaw_rate"]), control=np.asarray(ego["control"], np.float32),
                       size=np.asarray(ego["size"], np.float32))
        result = {"frame": int(snapshot.frame), "timestamp": float(snapshot.timestamp.elapsed_seconds),
                "coordinate_frame": "ego_flu", "ego": ego_obs, "objects": objects,
                "route": {"points": route, "valid": route_valid, "progress": self.progress,
                          "length": float(self.route_s[-1]), "index": self.route_index},
                "signals": self._signals(origin, yaw), "obstacles": self._obstacles(origin, yaw),
                "map_id": self.static["map_id"], "sensors": sensors or {}}
        if self.planning:
            from .planning import planning_observation
            result["planning"] = planning_observation(self)
            result["route_id"] = self.route_description["route_id"]
        return result

    def update_route(self, origin):
        if getattr(self, "_route_frame", None) == self.frame_state.frame:
            return
        self._route_frame = self.frame_state.frame
        if len(self.route):
            # Search locally to avoid jumping to another branch of a crossing route.
            stop = min(len(self.route), self.route_index + 50)
            begin = max(0, self.route_index - 5)
            self.route_index = begin + int(np.argmin(np.linalg.norm(self.route[begin:stop] - origin, axis=1)))
        if self.route_description is not None:
            self.route_segment_index = int(self.route_description["point_segment_index"][self.route_index])

    def task_state(self):
        """Only reward/termination inputs; called at EVERY physics tick."""
        ego = self.get_state(self.episode.ego)
        origin, yaw = np.asarray(ego["position"]), ego["yaw"]
        self.update_route(origin)
        return {"route": {"progress": self.progress},
                "ego": {"acceleration": local(ego["acceleration"], origin, yaw, vector=True),
                        "yaw_world": yaw, "speed": ego["speed"], "speed_limit": ego["speed_limit"]}}

    @property
    def progress(self):
        return float(self.route_s[min(self.route_index, len(self.route_s) - 1)])

    def _signals(self, origin, yaw):
        cap = self.config["max_signals"]
        rows = []
        for signal in self.frame_state.signals:
            if np.linalg.norm(np.asarray(signal["trigger_position"]) - origin) > self.config["range"]:
                continue
            position = np.asarray(signal["position"])
            rows.append((np.linalg.norm(position - origin), signal["actor_id"], position,
                         signal["state"], signal["direction"], signal["stop_line"], signal["lane_ids"]))
        rows.sort(key=lambda x: (x[0], x[1]))
        positions, states, valid = np.zeros((cap, 3), np.float32), np.zeros(cap, np.int32), np.zeros(cap, bool)
        directions, lines = np.zeros((cap, 3), np.float32), np.zeros((cap, 2, 3), np.float32)
        lane_ids, ids = np.zeros((cap, 3), np.int32), np.zeros(cap, np.int64)
        for i, (_, aid, p, state, direction, line, lanes) in enumerate(rows[:cap]):
            positions[i], states[i], valid[i] = local(p, origin, yaw), state, True
            directions[i], lines[i] = local(direction, origin, yaw, vector=True), local(line, origin, yaw)
            lane_ids[i], ids[i] = lanes, aid
        result = {"position": positions, "direction": directions, "stop_line": lines,
                "lane_ids": lane_ids, "ids": ids, "state": states, "valid": valid,
                "truncated": max(0, len(rows) - cap), "position_kind": "stop_waypoint"}
        if self.planning:
            association = np.full(cap, -1, np.int32)
            known = np.zeros(cap, bool)
            for i in np.flatnonzero(valid):
                matches = np.flatnonzero(np.all(self.static["lane_ids"] == lane_ids[i], axis=1))
                if len(matches):
                    world_position = rows[i][2]
                    distances = [(np.min(np.linalg.norm(self.static["center"][self.static["offsets"][j]:self.static["offsets"][j+1]]-world_position, axis=1)),j) for j in matches]
                    distance,j = min(distances)
                    if distance <= 4.: association[i],known[i] = j,True
            result.update(segment_index=association, association_valid=known)
        return result

    def _obstacles(self, origin, yaw):
        cap = self.config["max_obstacles"]
        rows = []
        for obstacle in self.frame_state.obstacles:
            position = np.asarray(obstacle["position"])
            distance = np.linalg.norm(position - origin)
            if distance <= self.config["range"]:
                rows.append((distance, obstacle["actor_id"], position, obstacle["size"]))
        rows.sort(key=lambda x: (x[0], x[1]))
        positions, sizes, valid = np.zeros((cap, 3), np.float32), np.zeros((cap, 3), np.float32), np.zeros(cap, bool)
        for i, (_, _, position, size) in enumerate(rows[:cap]):
            positions[i], sizes[i], valid[i] = local(position, origin, yaw), size, True
        return {"position": positions, "size": sizes, "valid": valid, "truncated": max(0, len(rows) - cap)}

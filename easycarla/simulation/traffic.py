"""Crossing-based rule events from the same committed frame as observations."""
import numpy as np


class TrafficRules:
    def __init__(self, session, ego):
        self.session, self.ego = session, ego
        self.previous = {}
        self.stopped = set()

    def update(self, frame_state):
        state = frame_state.actors[self.ego.id]
        pos, velocity = np.asarray(state["position"]), np.asarray(state["velocity"])
        violations = []
        for signal in frame_state.signals:
            anchor = np.asarray(signal["position"])
            forward, lateral = np.asarray(signal["direction"]), np.asarray(signal["right"])
            delta = pos - anchor
            longitudinal = float(delta @ forward)
            side = abs(float(delta @ lateral))
            road, _, lane = signal["lane_ids"]
            key = (signal["actor_id"], road, lane)
            previous = self.previous.get(key)
            in_lane = side < signal["lane_width"] / 2
            if in_lane and -6 < longitudinal <= 0 and np.linalg.norm(velocity) < 0.1:
                self.stopped.add(key)
            if previous is not None and previous < 0 <= longitudinal and in_lane and float(velocity @ forward) > 0:
                if signal["prohibited"] and (signal["type"] != "traffic.stop" or key not in self.stopped):
                    violations.append(f"{key[0]}:{road}:{lane}")
            self.previous[key] = longitudinal
        return violations

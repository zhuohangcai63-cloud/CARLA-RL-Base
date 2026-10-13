"""Same physics facts/rewards/events, fewer complete observation assemblies."""

import numpy as np
from test_runtime_core import runtime_factory, FakeObserver
from easycarla.runtime import EnvironmentRuntime


def test_deferred_task_inputs_preserve_reward_ticks_and_terminal(
    monkeypatch, runtime_factory
):
    builds = []
    original = FakeObserver.build

    def build(self, snapshot, sensors):
        builds.append(snapshot.frame)
        return original(self, snapshot, sensors)

    def task_state(self):
        frame = self.samples[-1][0]
        return {
            "route": {"progress": float(frame - 100)},
            "ego": {"acceleration": np.zeros(3), "speed": 0.0, "speed_limit": 10.0},
        }

    monkeypatch.setattr(FakeObserver, "build", build)
    monkeypatch.setattr(FakeObserver, "task_state", task_state, raising=False)
    config = {"task": {"max_seconds": 0.3}}
    legacy = EnvironmentRuntime(config)
    fast = EnvironmentRuntime(
        {**config, "performance": {"deferred_observations": True}}
    )
    for env in (legacy, fast):
        env._facts = lambda: {"collision": False, "offroad": False, "goal": False}
    try:
        legacy.reset(seed=1)
        fast.reset(seed=1)
        builds.clear()
        a = legacy.step([0, 0, 1])
        legacy_builds = len(builds)
        builds.clear()
        b = fast.step([0, 0, 1])
        fast_builds = len(builds)
        assert legacy_builds == 4 and fast_builds == 1
        assert (
            a[1:4] == b[1:4] and a[4]["reward_components"] == b[4]["reward_components"]
        )
        assert a[4]["physical_ticks"] == b[4]["physical_ticks"] == 4
        assert legacy.observer.samples == fast.observer.samples
        builds.clear()
        a = legacy.step([0, 0, 1])
        b = fast.step([0, 0, 1])
        assert (
            a[1:4] == b[1:4]
            and a[3]
            and a[4]["physical_ticks"] == b[4]["physical_ticks"] == 2
        )
        assert a[0]["frame"] == b[0]["frame"] == 107
        assert a[4]["reward_components"] == b[4]["reward_components"]
    finally:
        legacy.shutdown()
        fast.shutdown()

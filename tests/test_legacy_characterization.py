"""Preserved behavior at the migration boundary (no running CARLA needed)."""
from types import SimpleNamespace
import pytest


def test_mixed_template_expands_only_three_existing_event_families():
    pytest.importorskip("carla")
    from easycarla.scenarios.long_tail_events import LongTailEventManager
    manager = LongTailEventManager(SimpleNamespace(town_name="Town10HD"), seed=10)
    specs = manager._expand_event_specs([{"type": "mixed_long_tail", "enabled_prob": 1.0}])
    assert {s["type"] for s in specs} == {"parking_cut_in", "vehicle_opens_door", "construction_obstacle"}
    assert all(s["enabled_prob"] == 1 for s in specs)


@pytest.mark.parametrize("filename", ["pedestrian_crossing", "hard_break_route", "parking_cut_in",
                                     "construction_obstacle", "vehicle_opens_door", "mixed_long_tail"])
def test_existing_scenic_templates_compile_and_sample(filename):
    scenic = pytest.importorskip("scenic")
    scenario = scenic.scenarioFromFile(f"scenarios/scenic/{filename}.scenic")
    scene, _ = scenario.generate(maxIterations=2000)
    assert scene.params["scenario_name"] == filename
    assert scene.params["min_route_distance"] > 0
    assert scene.params["number_of_vehicles"] >= 0


@pytest.mark.parametrize("speed,offset,lat_acc,red,collision,current,previous,waypoint,expected", [
    (8, .2, .1, False, False, 100, 102, 1, .554),
    (0, 0, 0, True, False, 100, 100, 0, .2),
    (10, 0, 0, False, True, 100, 100, 0, -1.8),
    (5, 0, 0, False, False, 1.5, 2.5, 0, 4.2),
])
def test_frozen_v0_reward_reference(speed, offset, lat_acc, red, collision, current, previous, waypoint, expected):
    import runpy
    reward = runpy.run_path("tests/fixtures/legacy_reward_v0.py")["legacy_reward"]
    env = SimpleNamespace(desired_speed=10, is_red_light_present=red, _is_collision=collision,
                          _is_off_road=False, ego=SimpleNamespace(get_location=lambda: SimpleNamespace(distance=lambda _: current)),
                          target_destination=None, prev_dist_to_goal=previous, last_closest_idx=waypoint, last_rewarded_idx=0)
    obs = {"ego_state": [0, 0, 0, speed, 0, 0, lat_acc, -1], "lane_info": [4, offset, 0]}
    assert reward(env, obs, False) == pytest.approx(expected)

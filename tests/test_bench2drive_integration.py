"""Opt-in real-server tests; excluded unless EASY_CARLA_B2D_INTEGRATION=1.

Use a dedicated server, never an instance controlled by another environment.
The JSON report distinguishes execution, activation, completion and failures.
"""
import json
import os
from pathlib import Path
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("EASYCARLA_B2D_INTEGRATION") != "1",
                                reason="requires an explicitly assigned real CARLA server")


def test_native_representative_routes():
    from easycarla.config import validate_config
    from easycarla.simulation.session import WorldSession
    from easycarla.scenarios.bench2drive import Bench2DriveAdapter
    root = Path(__file__).resolve().parents[1]
    cfg = validate_config({"simulation": {"port": int(os.environ.get("EASYCARLA_B2D_PORT", "2100")),
                                         "tm_port": int(os.environ.get("EASYCARLA_B2D_TM_PORT", "8100")),
                                         "timeout": float(os.environ.get("EASYCARLA_B2D_TIMEOUT", "30")),
                                         "no_rendering_mode": os.environ.get("EASYCARLA_B2D_NO_RENDERING", "0") == "1",
                                         "map_layers": os.environ.get("EASYCARLA_B2D_MAP_LAYERS", "all"),
                                         "reload_on_reset": os.environ.get("EASYCARLA_B2D_RELOAD", "1") == "1"}})
    scenario = {"manifest": str(root / "configs/manifests/v004_eval220.json"), "purpose": "eval"}
    if os.environ.get("EASYCARLA_B2D_OPT_TOWN10") == "1":
        scenario["town_overrides"] = {"Town10HD": "Town10HD_Opt"}
    adapter = Bench2DriveAdapter(scenario)
    session = WorldSession(cfg)
    from agents.navigation.behavior_agent import BehaviorAgent
    import carla
    from agents.navigation.local_planner import RoadOption
    route_ids = os.environ.get("EASYCARLA_B2D_ROUTES", "24416,22535,3816").split(",")
    max_ticks = int(os.environ.get("EASYCARLA_B2D_TICKS", "1000"))
    output = Path(os.environ.get("EASYCARLA_B2D_REPORT", str(root / "docs/testing/bench2drive-real.json")))
    output.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    try:
        for route_id in route_ids:
            started = time.monotonic()
            result = {"route_id": route_id, "seed": 123, "status": "running", "ticks": 0,
                      "activated": [], "completed": [], "native_done": False, "native_success": False}
            episode = None
            try:
                options = {"route_id": route_id}
                town = adapter.town(123, options)
                result["town"] = town
                print(f"B2D route {route_id}: loading {town}", flush=True)
                session.load(town, 123)
                print(f"B2D route {route_id}: constructing native scene", flush=True)
                episode = adapter.prepare(session, 123, options)
                session.controls.handoff(episode.ego, "reference_driver")
                print(f"B2D route {route_id}: executing", flush=True)
                result["initialize_seconds"] = time.monotonic() - started
                result["initialization_ticks"] = session.ticks["initialization"]
                result["capabilities"] = episode.capabilities()
                agent = BehaviorAgent(episode.ego, behavior="normal")
                agent.set_global_plan([(session.map.get_waypoint(t.location), RoadOption.LANEFOLLOW)
                                       for t in episode.route])
                idle_frame = session.world.get_snapshot().frame
                time.sleep(0.2)
                assert session.world.get_snapshot().frame == idle_frame, "world advanced without a request"
                activated, completed = set(), set()
                transformed = 0
                for index in range(max_ticks):
                    episode.before_tick()
                    control = carla.VehicleControl(brake=1.0) if agent.done() else agent.run_step()
                    requested = np.array([control.steer, control.throttle, control.brake], dtype=np.float32)
                    resolved = episode.resolve_control(requested)
                    applied = resolved.values
                    transformed += int(not np.array_equal(requested, applied))
                    session.controls.apply_control(episode.ego, applied, owner="reference_driver",
                                                   allow_combined=resolved.allow_combined)
                    snapshot = session.tick()
                    episode.after_tick(snapshot)
                    info = episode.info()
                    result["info"] = info
                    result["ticks"] += 1
                    for item in info["native_scenarios"]:
                        if "RUNNING" in item["status"]:
                            activated.add(item["name"])
                        if "SUCCESS" in item["status"]:
                            completed.add(item["name"])
                    result["activated"], result["completed"] = sorted(activated), sorted(completed)
                    if info["native_done"]:
                        break
                result.update(status="passed", activated=sorted(activated), completed=sorted(completed),
                              native_done=info["native_done"], native_success=info["native_success"],
                              native_control_transforms=transformed, info=info,
                              simulation_seconds=session.timings["simulation_seconds"],
                              native_ticks=result["ticks"], managed_step_ticks=session.ticks["step"])
                assert session.ticks["step"] == result["ticks"], "hidden runtime tick detected"
            except Exception as exc:
                import traceback
                result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
            finally:
                if episode is not None:
                    try:
                        episode.close()
                    except Exception as exc:
                        result.update(status="failed", cleanup_error=str(exc))
                result["wall_seconds"] = time.monotonic() - started
                reports.append(result)
                output.write_text(json.dumps(reports, indent=2) + "\n")
    finally:
        session.close()
    assert all(r["status"] == "passed" for r in reports), json.dumps(reports, indent=2)

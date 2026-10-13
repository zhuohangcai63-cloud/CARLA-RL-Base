"""Opt-in real two-Conda, shared-memory Bench2Drive service acceptance.

The client program imports only its lightweight package, NumPy and stdlib.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("EASYCARLA_B2D_SERVICE") != "1",
                                reason="requires dedicated live CARLA and a separate client Python")

CLIENT_PROGRAM = r'''
import importlib.util,json,math,sys,time
from pathlib import Path
import numpy as np
from easycarla_client import CarlaEnvClient

def reference_control(obs):
    points=obs["route"]["points"][obs["route"]["valid"]]
    speed=float(obs["ego"]["speed"])
    if not len(points): return [0,0,1]
    distances=np.linalg.norm(points[:,:2],axis=1)
    candidates=np.flatnonzero((distances>max(4.0,speed*0.6)) & (points[:,0]>0))
    target=points[int(candidates[0])] if len(candidates) else points[-1]
    alpha=math.atan2(float(target[1]),float(target[0]))
    steer=float(np.clip(-math.atan2(5.6*math.sin(alpha),max(1,float(np.linalg.norm(target[:2]))))/0.65,-1,1))
    target_speed=5.0
    if obs["route"]["length"]-obs["route"]["progress"]<5: target_speed=1.0
    error=target_speed-speed
    return [steer,float(np.clip(error*0.3,0,0.5)),float(np.clip(-error*0.2,0,0.6))]

report={"client_python":sys.executable,"client_has_forbidden_dependencies":{
    name: importlib.util.find_spec(name) is not None for name in ("carla","scenic","torch")},"episodes":[]}
assert not any(report["client_has_forbidden_dependencies"].values())
env=CarlaEnvClient(sys.argv[1],timeout=90)
try:
 for route,budget in [("28147",10),("25134",10),("28147",int(sys.argv[3]))]:
    obs,info=env.reset(seed=123,options={"route_id":route})
    episode={"route_id":route,"reset_frame":obs["frame"],"source":info["scenario"]["source"],
             "scenario":info["scenario"],"observation_keys":sorted(obs),"steps":0,
             "object_count":int(obs["objects"]["count"]),"max_object_count":0,
             "terminated":False,"truncated":False,"native_success":False}
    assert episode["source"]=="bench2drive"
    assert obs["coordinate_frame"]=="ego_flu"
    count=obs["objects"]["count"]
    assert len(set(obs["objects"]["ids"][:count].tolist()))==count
    assert obs["objects"]["position"].shape==(21,64,3)
    assert obs["objects"]["position"].dtype==np.float32
    assert obs["objects"]["valid"].dtype==np.bool_
    frozen=obs["objects"]["position"].copy()
    first_observation=obs
    static=env.get_static(obs["map_id"])
    assert static["map_id"]==obs["map_id"]
    assert env.get_static(obs["map_id"])["map_id"]==static["map_id"]
    episode["static_keys"]=sorted(static)
    episode["capabilities"]=env.capabilities()
    time.sleep(0.1)
    roundtrips=[];transports=[];simulations=[]
    for step in range(budget):
        previous_frame=obs["frame"]
        action=reference_control(obs) if budget>10 else [0,0.15,0]
        obs,reward,terminated,truncated,info=env.step(action)
        delta=obs["frame"]-previous_frame
        assert 1<=delta<=4
        if not terminated and not truncated: assert delta==4
        assert np.array_equal(frozen,first_observation["objects"]["position"])
        count=obs["objects"]["count"]
        assert len(set(obs["objects"]["ids"][:count].tolist()))==count
        episode["max_object_count"]=max(episode["max_object_count"],int(count))
        roundtrips.append(env.last_timings["roundtrip_seconds"])
        transports.append(env.last_timings["transport_and_copy_seconds"])
        simulations.append(info["timing"].get("simulation_seconds",0))
        episode["steps"]+=1
        episode.update(terminated=bool(terminated),truncated=bool(truncated),final_frame=obs["frame"],
                       native_success=info["events"]["native_success"],tick_counts=info["tick_counts"],
                       native_statistics=info["events"]["native_statistics"],
                       native_scenarios=info["events"]["native_scenarios"])
        if terminated or truncated:break
    episode["timings"]={name:{"p50":float(np.percentile(values,50)),"p95":float(np.percentile(values,95))}
                        for name,values in [("roundtrip_seconds",roundtrips),("transport_and_copy_seconds",transports),("simulation_seconds",simulations)]}
    report["episodes"].append(episode)
finally:
 env.close()
report["status"]="passed"
Path(sys.argv[2]).write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps({"status":report["status"],"episodes":[{k:e[k] for k in ["route_id","steps","native_success"]} for e in report["episodes"]]}))
'''


def test_real_bench2drive_two_conda_service(tmp_path):
    from easycarla_client.transport import Names, read_live_record
    root = Path(__file__).resolve().parents[1]
    client_python = os.environ.get("EASYCARLA_CLIENT_PYTHON")
    assert client_python and Path(client_python).is_file(), "Set EASYCARLA_CLIENT_PYTHON to the independent Conda Python"
    instance = f"b2d_acceptance_{os.getpid()}"
    config = {"simulation": {"port": int(os.environ.get("EASYCARLA_B2D_PORT", "2100")),
                             "tm_port": int(os.environ.get("EASYCARLA_B2D_TM_PORT", "8100")),
                             "reload_on_reset": False, "timeout": 30},
              "scenario": {"source": "bench2drive", "purpose": "eval", "native_metrics": True,
                           "manifest": str(root / "configs/manifests/v004_eval220.json"),
                           "town_overrides": {"Town10HD": "Town10HD_Opt"}},
              "task": {"profile": "bench2drive_native", "max_seconds": 300}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    client_path = tmp_path / "client.py"
    client_path.write_text(CLIENT_PROGRAM)
    output = root / "docs/testing/bench2drive-service-real.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = root / "docs/testing/bench2drive-service.log"
    with log_path.open("w") as log:
        service = subprocess.Popen([sys.executable, "-u", "-m", "easycarla.service.cli", "serve",
                                    "--instance", instance, "--config", str(config_path)], cwd=root,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert service.poll() is None, log_path.read_text()
                record = read_live_record(Names(instance).server_lock)
                if record and record.get("ready"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Service did not become ready")
            result = subprocess.run([client_python, str(client_path), instance, str(output),
                                     os.environ.get("EASYCARLA_B2D_REFERENCE_STEPS", "500")],
                                    cwd=tmp_path, capture_output=True, text=True, timeout=300)
            assert result.returncode == 0, result.stdout + result.stderr + log_path.read_text()
            assert service.poll() is None, "Client close terminated the service"
        finally:
            service.terminate()
            service.wait(timeout=30)
    report = json.loads(output.read_text())
    report["service_python"] = sys.executable
    report["server_port"] = config["simulation"]["port"]
    report["service_exit_code"] = service.returncode
    report["client_stdout"] = result.stdout
    report["config"] = config
    import carla
    server = carla.Client(config["simulation"]["host"] if "host" in config["simulation"] else "127.0.0.1",
                          config["simulation"]["port"])
    server.set_timeout(3)
    remaining = [a.type_id for a in server.get_world().get_actors()
                 if a.type_id.startswith(("vehicle.", "sensor.", "walker.", "controller."))]
    report["remaining_dynamic_actors"] = remaining
    report["ipc_service_record"] = read_live_record(Names(instance).server_lock)
    output.write_text(json.dumps(report, indent=2) + "\n")
    assert remaining == []
    assert not report["ipc_service_record"]
    assert service.returncode == 0, "Service shutdown must handle semaphore signal interruption cleanly"

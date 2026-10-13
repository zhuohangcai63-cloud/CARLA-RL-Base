# CARLA-RL-Base / EasyCARLA

English | [简体中文](README.zh-CN.md)

**A CARLA Gymnasium environment platform for reinforcement learning and interaction research.** The environment handles simulation, scenarios, observations, actions, rewards and episode lifecycle. Your models and training code run in a separate project and connect through same-machine shared memory.

The repository is `CARLA-RL-Base`; the service package and command are `easycarla`; the independently installable client package is `easycarla-client`, imported as `easycarla_client`.

For a first run, follow **install both sides → test the fake backend → connect one CARLA server → use Gymnasium → add parallel workers**. Bench2Drive, planner models and training libraries are optional and unnecessary for the first steps.

## Contents

- [Architecture and features](#architecture-and-features)
- [Requirements and installation](#requirements-and-installation)
- [Step 1: Check communication without CARLA](#step-1-check-communication-without-carla)
- [Step 2: Run one real CARLA environment](#step-2-run-one-real-carla-environment)
- [Step 3: Use Gymnasium](#step-3-use-gymnasium)
- [Step 4: Run parallel headless workers](#step-4-run-parallel-headless-workers)
- [Observations, clocks and trajectory actions](#observations-clocks-and-trajectory-actions)
- [Choose scenarios and configurations](#choose-scenarios-and-configurations)
- [Failures and troubleshooting](#failures-and-troubleshooting)
- [Tests, measured coverage and extension points](#tests-measured-coverage-and-extension-points)

## Architecture and features

```text
Your policy / training project (its own Python environment)
        │
        └── easycarla-client: raw client / Gymnasium / vector interface
                    │  Linux shared memory + named semaphores
             ┌──────┴──────┐
        Service 0       Service 1       …
             │               │
          CARLA 0          CARLA 1
```

Each instance owns one environment service and one exclusive CARLA world, with one controlling client at a time. Parallel sampling uses independent CARLA processes. The world waits while the policy computes its next action.

| Capability | Implementation |
|---|---|
| Scenarios | Scenic, Bench2Drive and explicitly configured episode-level source mixing |
| Scenic events | Pedestrian crossing, lead braking, parking cut-in, opening doors and construction obstacles; generation, activation, completion and skip reasons |
| Actions | Steering/throttle/brake and external trajectory tracking with actor or rear-axle reference |
| Observations | Structured truth, object history, routes, signals and cached maps; optional `planning_v1` geometry, ego history, directed lanes and route corridors |
| Sensors | Optional RGB, semantic-camera BEV and LiDAR; all three disabled in the fast headless profile |
| Tasks | General driving rewards and termination, plus a separate Bench2Drive native evaluation profile; RL rewards and native metrics reported separately |
| Parallel execution | Isolated workers, fleet lifecycle and explicit restart, Gymnasium vectors and completion-order results |
| Research extensions | Optional reactive NPCs, mechanism switching, public-prefix recording/reconstruction/replay and tolerance-based comparisons |

There is no PPO, replay buffer, model inference or Flow Planner implementation. `planning_v1` supplies general data and an external field mapping; model adaptation, normalization and driving validation belong to your policy project. Structured observations are CARLA ground truth rather than estimated perception or Bench2Drive sensor-track competition inputs.

## Requirements and installation

| Component | Environment | What to install |
|---|---|---|
| OS | Linux; service and client on the same machine | POSIX shared memory and named semaphores; the current transport is not a cross-machine network interface |
| Service | Python 3.10 recommended | This repository and the client package; CARLA/Scenic for real simulation, optional Bench2Drive dependencies |
| Simulator | Official CARLA 0.9.15 | Server binaries, corresponding maps and `PythonAPI/carla/agents`; rendering requires working GPU drivers |
| External client | Examples use Python 3.11; package requires Python ≥3.10 | NumPy and `posix_ipc`; optional Gymnasium; no CARLA, Scenic or Torch requirement |

Run commands from the repository root. If you already have a checkout, use its directory:

```bash
git clone https://github.com/zhuohangcai63-cloud/CARLA-RL-Base.git
cd CARLA-RL-Base
```

### Install the service

Start with the base service, which can run the fake backend:

```bash
conda env create -f environments/service.yml
conda activate easycarla-service
python -m pip install -c environments/constraints-service.txt \
  -e ./packages/client -e '.[test]'
python -m pip check
easycarla --help
```

Before running real Scenic scenarios, add the simulator dependencies in the same environment:

```bash
python -m pip install -c environments/constraints-service.txt -e '.[carla,test]'
```

The `carla` extra installs the CARLA 0.9.15 Python API and Scenic 3.1.1. **It does not download simulator binaries or maps.** The Conda YAML files only create base environments; the pip step is required. You may reuse an existing service environment. The old `environment.yml` remains a compatibility entry point.

### Install the separate client

In another terminal, from the repository root:

```bash
conda env create -f environments/client.yml
conda activate easycarla-client
python -m pip install -c environments/constraints-client.txt ./packages/client
python -c "from easycarla_client import CarlaEnvClient; print('client import OK')"
```

Your training project only needs `packages/client`. For Gymnasium support, install the extra:

```bash
python -m pip install -c environments/constraints-client.txt './packages/client[gym]'
```

The training environment does not need the `easycarla` service package. Use an absolute client-package path when installing from another directory; wheel distribution is also supported. See [installation details](docs/installation.md) for dependencies, path resolution and packaging.

## Step 1: Check communication without CARLA

This checks installation, cross-process connection and `reset/step/close`. **The fake backend has no driving physics, real scenarios or planning observations.** It needs no GPU, CARLA server or Bench2Drive checkout.

Terminal A, service environment; leave it running:

```bash
conda activate easycarla-service
easycarla serve --instance protocol_test --config configs/fake.yaml
```

Terminal B, client environment:

```bash
conda activate easycarla-client
python examples/client_loop.py --instance protocol_test --episodes 1 --steps 5
```

Expect one `reset` line followed by five `step` lines with frames 4, 8, 12, 16 and 20. The example releases its client connection on exit; the service stays running. Stop the service with Ctrl-C in terminal A. Resolve installation or IPC errors here before debugging real simulation.

## Step 2: Run one real CARLA environment

Manual execution has **three processes**: simulator, service and client. `serve` connects to an existing simulator. The fleet workflow below can launch and manage the simulator processes for you.

### 1. Check paths and configuration

Install CARLA 0.9.15 and Town10HD, then check [configs/scenic.yaml](configs/scenic.yaml):

- `simulation.carla_root`: your CARLA installation; the example uses `~/software/carla`.
- `simulation.port`: CARLA RPC port, `2000` in the example.
- `simulation.tm_port`: exclusive Traffic Manager port, `8000` in the example.
- `scenario.town` and `scenario.file`: Town10HD and the Scenic long-tail scenario.

An explicit `simulation.carla_root` takes precedence over `CARLA_ROOT`, then the default `~/software/carla`. Changing the environment variable does not override a path already in YAML. The service locates CARLA agents through this installation; the client needs no simulator `PYTHONPATH`.

### 2. Terminal A: Start CARLA

```bash
# Replace this path with your simulator installation.
~/software/carla/CarlaUE4.sh \
  -RenderOffScreen -nosound -quality-level=Epic -carla-rpc-port=2000
```

`-RenderOffScreen` still renders without showing a window; it is not the purely structured no-rendering mode. The example uses `Epic` because this workstation reproduced a Town03 Vulkan crash at `Low`. This does not guarantee every map is stable. The fast fleet workflow uses `-nullrhi`.

### 3. Terminal B: Start the service

```bash
conda activate easycarla-service
easycarla serve --instance carla_01 --config configs/scenic.yaml
```

Map and scenario initialization happens when the client calls `reset()`. A running service does not imply an initialized episode.

### 4. Terminal C: Connect and step

```bash
conda activate easycarla-client
python examples/client_loop.py --instance carla_01 --episodes 1 --steps 10 --timeout 180
```

Or use the raw client from your project:

```python
from easycarla_client import CarlaEnvClient

with CarlaEnvClient("carla_01", timeout=180) as env:
    obs, info = env.reset(seed=7)
    static_map = env.get_static(obs["map_id"])
    for _ in range(10):
        # Communication example, not a policy that follows the route.
        obs, reward, terminated, truncated, info = env.step([0.0, 0.15, 0.0])
        print(obs["frame"], reward, terminated, truncated)
        if terminated or truncated:
            break
```

The library request timeout defaults to 30 seconds; the example script defaults to 180 seconds for cold map initialization. Client `timeout` covers the entire request. Service `simulation.timeout` controls CARLA RPC waits.

On exit, close the client, stop the service with Ctrl-C, then stop manually launched CARLA. `env.close()` ends the client session and episode without stopping the service or external simulator.

## Step 3: Use Gymnasium

With the client `[gym]` extra installed, construct `CarlaGymEnv` directly. No `gym.make()` or service imports are required:

```python
from easycarla_client import CarlaGymEnv

with CarlaGymEnv("carla_01", timeout=180) as env:
    print(env.observation_space, env.action_space)
    obs, info = env.reset(seed=7)
    for _ in range(10):
        obs, reward, terminated, truncated, info = env.step([0.0, 0.15, 0.0])
        if terminated or truncated:
            break  # Explicitly reset before starting another episode.
```

| Interface | Observation format | Use |
|---|---|---|
| `CarlaEnvClient` | Nested dictionaries, e.g. `obs["ego"]["velocity"]` | Custom adapters, full queries and minimal dependencies |
| `CarlaGymEnv` | Fixed-shape numeric/mask Dict, e.g. `obs["ego.velocity"]` | Gymnasium training projects |
| `CarlaVectorEnv` | The same fixed fields with a leading worker dimension | Synchronous batch sampling |
| `ClientPool` | Worker IDs and raw results in completion order | External sampling schedules and slow-worker handling |

Gym puts strings such as `map_id/route_id` in `info`. Query static data through `env.get_static(info["map_id"])` and `env.get_route(info["route_id"])`. Spaces are published by the service before reset and remain fixed. Raw nested keys and Gym dotted paths are different interfaces.

## Step 4: Run parallel headless workers

Complete single-instance setup first. Fleet starts independent CARLA/service processes with `-nullrhi -nosound` and verifies `no_rendering_mode=true`. Fast workers disable RGB, semantic BEV and LiDAR.

### Configure the fleet

In the service environment, copy the example into the same directory so its relative worker paths stay valid:

```bash
conda activate easycarla-service
cp configs/fast/fleet.yaml configs/fast/fleet.local.yaml
python -c "import sys; print(sys.executable)"
```

Edit `configs/fast/fleet.local.yaml`:

| Field | Check |
|---|---|
| `service_python` | Absolute interpreter path printed above; use the service environment |
| `carla_root` | CARLA 0.9.15 installation; worker installation paths should agree |
| `log_dir` | Supervisor, CARLA and service logs, plus persisted status |
| `workers[].config` | Both example workers use `scenic_train.yaml` on Town10HD |
| `workers[].id / instance` | Management ID / connection name: `worker_0 / planning_0`, `worker_1 / planning_1` |
| `workers[].ports` | Exclusive RPC, streaming, secondary and TM ports, distinct across workers |
| Memory/thread budgets | Estimated 6000 MiB per worker, 4096 MiB reserved, numerical-library threads 1; adjust for your machine |

The example's `/home/czh/...` paths are workstation-specific and must be changed. Memory budgets are admission estimates, not hard limits. Start with one or two workers.

```bash
conda activate easycarla-service
easycarla fleet start --config configs/fast/fleet.local.yaml
easycarla fleet status --config configs/fast/fleet.local.yaml
```

### Submit actions from the client

```python
import numpy as np
from easycarla_client import CarlaVectorEnv

env = CarlaVectorEnv(["planning_0", "planning_1"], timeout=180)
try:
    obs, infos = env.reset(seed=7)  # Worker i uses seed+i; a seed list is also accepted.
    actions = np.tile([0.0, 0.0, 1.0], (env.num_envs, 1)).astype(np.float32)
    env.step_async(actions)
    obs, rewards, terminated, truncated, infos = env.step_wait()
    done = terminated | truncated
    if done.any():
        obs, infos = env.reset(mask=done)  # Preserve unselected workers' observations.
finally:
    env.close()
```

Workers in one vector must share observation/action specifications and clocks. Each worker permits one outstanding request. All batch inputs are locally checked before publication. Autoreset is disabled and terminal observations are preserved. Synchronous vectors wait for the full batch; use [ClientPool](docs/parallel-environments.md#完成顺序与故障) to consume independent results in completion order.

### Restart and stop

Close the affected client before explicitly restarting a failed worker, then reconnect and reset:

```bash
easycarla fleet restart --config configs/fast/fleet.local.yaml --worker worker_0
easycarla fleet stop --config configs/fast/fleet.local.yaml
```

Fleet only stops its registered process groups, not manually started CARLA. `status/stop` work even if worker configs, interpreters or simulator installations are missing; preserve fleet identities, log paths and process records. Restart validates the new startup configuration before stopping the old worker. Unknown actions are never replayed.

Official CARLA 0.9.15 does not implement CaRL's custom RPC/streaming/secondary thread limits. Requested budgets are recorded as unapplied; service numerical-library limits are applied. `-nothreading` is off by default. See [parallel lifecycle details](docs/parallel-environments.md).

## Observations, clocks and trajectory actions

### Controls and episode results

`step()` returns `(obs, reward, terminated, truncated, info)`. Reward sums per-physics-tick terms across the decision window. `terminated` means natural task completion; `truncated` means task time limits. Simulator crashes, missing required sensors and unknown outcomes raise errors instead of artificial truncated transitions.

Actions are `[steer, throttle, brake]`: steer in `[-1,1]`, **positive is right**; throttle/brake in `[0,1]`. Simultaneous positive throttle and brake, wrong shapes, nonfinite values and out-of-range controls are rejected without clipping. Normalized steer is not an angle in radians.

| Configuration | Physics interval | Decision window | History interval |
|---|---:|---:|---:|
| Existing defaults | 0.05 s | 0.2 s, usually 4 ticks | 0.1 s |
| `configs/fast/` | 0.05 s | 0.1 s, usually 2 ticks | 0.1 s |

Termination ends a window early. Inspect `info.physical_ticks` and `info.executed_seconds` for actual advancement. Map/route/capability queries and client inference do not advance the world.

### legacy and planning_v1

The default `legacy` profile keeps actor-origin semantics. Optional `planning_v1` adds real ego history, rear-axle geometry, object bounding-box centers/history, separate bicycle/motorcycle types, static objects, directed lane segments and route corridors.

Planning fields use the **current ego rear axle** as origin, x forward and y left. History is transformed into this current frame. Position/size use meters, velocity m/s, acceleration m/s², angles radians and yaw rate rad/s. Size order is `[length,width,height]`. Neighbor velocities are rotated absolute velocities without subtracting ego velocity. Existing fields retain their coordinates.

21 samples at 0.1-second intervals represent two seconds of history plus the current sample. **Reset neither drives a hidden warmup nor fills missing history with duplicated states.** Collect history using ordinary `step/step_trajectory` calls; rewards, events and termination remain active. Check coverage and validity masks as well as `planning.history_ready`:

```python
from easycarla_client import CarlaGymEnv

# planning_0 is a running service configured with planning_v1.
with CarlaGymEnv("planning_0", timeout=180) as env:
    obs, info = env.reset(seed=7)
    while not bool(obs["planning.history_ready"]):
        obs, reward, terminated, truncated, info = env.step([0.0, 0.0, 1.0])
        if terminated or truncated:
            break  # An ended episode cannot be handed over to a planner.
    print("history_ready:", bool(obs["planning.history_ready"]))
```

Full history does not imply valid geometry or motion measurements. Maps/routes are cached by `map_id/route_id` rather than retransmitted on every step. Lane boundaries are waypoint-width estimates; CARLA corridors are not equivalent to nuPlan roadblocks. See [observations](docs/observations.md) and the [planning contract and Flow mapping](docs/planning-v1.md).

### Submit an external trajectory

The raw client supports `step_trajectory()`. This example requests a stationary trajectory on a real CARLA backend; it is not a planner:

```python
import numpy as np
from easycarla_client import CarlaEnvClient

with CarlaEnvClient("carla_01", timeout=180) as env:
    obs, info = env.reset(seed=7)
    trajectory = {
        "reference_frame": int(obs["frame"]),
        "reference_point": "actor",  # rear_axle needs planning_v1 and valid geometry.
        "times": np.array([0.0, 0.1, 0.2]),
        "points": np.zeros((3, 4)),  # [x, y, yaw, speed], in m / rad / m/s.
    }
    obs, reward, terminated, truncated, info = env.step_trajectory(trajectory)
```

Times start at zero and strictly increase; points must cover the execution window and speeds must be nonnegative. The reference frame must match the most recent observation. Gym's `action_mode="trajectory"` publishes a fixed action shape and generates times from configuration. See [trajectory details](docs/experiments.md) and [client semantics](docs/parallel-environments.md).

## Choose scenarios and configurations

| Configuration | Use |
|---|---|
| [fake.yaml](configs/fake.yaml) | Installation and IPC checks without CARLA |
| [scenic.yaml](configs/scenic.yaml) / [scenic_suite.yaml](configs/scenic_suite.yaml) | Default Scenic / explicit long-tail scenario suite |
| [fast/scenic_train.yaml](configs/fast/scenic_train.yaml) | planning_v1, fixed Town10HD, 10 Hz, training map reuse |
| [fast/scenic_eval.yaml](configs/fast/scenic_eval.yaml) | planning_v1, world reload, research extensions |
| [bench2drive_train.yaml](configs/bench2drive_train.yaml) | Town12 long training routes, general driving task |
| [fast/bench2drive_train.yaml](configs/fast/bench2drive_train.yaml) | Same train split, p6, planning_v1, map reuse, native task; 64 MiB static slot |
| [bench2drive_eval.yaml](configs/bench2drive_eval.yaml) | Pinned newer 220-route evaluation set and native evaluation rules |
| [bench2drive_low_load.yaml](configs/bench2drive_low_load.yaml) | Reduced Town12/13 streaming distances, rendering and full layers retained |
| [mixed.yaml](configs/mixed.yaml) | Episode-level Scenic/Bench2Drive mixing with explicit weights |
| [interaction.yaml](configs/interaction.yaml), [merge](configs/interaction_merge.yaml), [crossing](configs/interaction_crossing.yaml) | Reactive NPCs, fixed public paths and experiment extensions |

CLI asset paths default to the configuration's directory; top-level `path_base` changes that base. Ordinary examples use `..`, fast profiles use `../..`, both targeting the repository root. Adjust the base or use absolute paths when moving configs. Fleet worker paths are relative to the fleet file.

Training profiles use `reload_on_reset=false` to reuse the map while cleaning episode resources. Evaluation and prefix comparisons use strict reconstruction. Map reuse does not fully restore Traffic Manager hidden state or guarantee comparable initial conditions.

### Prepare Bench2Drive (optional)

Install native dependencies in the service environment and prepare an isolated copy from an existing upstream checkout:

```bash
python -m pip install -c environments/constraints-service.txt -e '.[carla,bench2drive,test]'
python scripts/prepare_bench2drive.py --source /path/to/Bench2Drive
```

The source must contain commit `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8`. Preparation creates `~/.cache/easycarla/bench2drive/<commit>-p6`, records dependency locks/patch hashes and preserves the source checkout. Set `scenario.dependency_root` for custom locations. Legacy configs may continue selecting an existing older copy; planning_v1 should explicitly select p6.

The 90 legacy Town12 long routes are split by geometry into **78 train / 12 dev**, not the newer 1100 short routes. Evaluation routes are never an implicit training fallback. Source failures are explicit unless configured otherwise. IDs in `reset(options={"route_id": "..."})` must belong to the selected manifest. See [Bench2Drive integration](docs/bench2drive.md) for metrics, takeover and replay limitations.

Once the copy and required maps are ready, stop the old service with the same instance name and start the training profile. You still manage the external CARLA server:

```bash
easycarla serve --instance carla_01 --config configs/bench2drive_train.yaml
```

Use `configs/bench2drive_eval.yaml` for native evaluation, or `configs/fast/bench2drive_train.yaml` as a fleet worker profile for headless planning training. Configuration changes require restarting the corresponding service; they are not hot-reloaded.

## Failures and troubleshooting

| Symptom | Check or response |
|---|---|
| `easycarla` command missing | Activate the service environment and pip-install the root package; Conda YAML alone is insufficient |
| Client or Gymnasium import fails | Install `packages/client` in the actual client interpreter, adding `[gym]` if needed |
| Cannot connect | Start the service and match its `--instance`; use the fake workflow to isolate IPC problems |
| Instance busy | Only one controlling client per instance; close the old connection before running another client |
| CARLA connection/reset fails | Check RPC port, server process, installation path and maps; inspect service/CARLA logs |
| Reset takes over 30 seconds | Increase client `timeout`, e.g. 180; distinguish cold initialization from RPC failures or crashes |
| Fast worker requests sensors | Disable RGB/BEV/LiDAR in fleet profiles; use a separate rendering instance for sensors |
| `history_ready=false` | History is incomplete or termination was off the history clock; inspect timestamps and validity masks |
| Static query exceeds capacity | Increase `ipc.static_bytes`; the Town12 planning map was about 18.8 MB, so its fast profile uses 64 MiB |
| Fleet startup rejected | Check paths, ports, duplicate identities, memory admission and no-rendering settings; inspect `log_dir` |
| Town12/13 native crash | Preserve exit codes/logs and consult map diagnostics; longer timeouts do not repair engine crashes |

Recovery semantics:

- **Terminal result:** retain the final observation and explicitly reset for the next episode.
- **Confirmed preflight rejection:** `RemoteError(code="invalid_request")` with `details.request_rejected=true` preserves the episode and availability. Correct the input, then issue a valid step or reset.
- **Published request times out / unknown outcome:** never automatically resubmit actions. Explicitly reconnect, reset or restart.
- **Partial batch failure:** `BatchError.completed` retains healthy results and `failures` identifies errors. Confirmed rejection does not disable a worker; other faults require explicit recovery.
- **Replacement:** reject self/shared-client replacements; legitimate replacement also requires explicit reset.

After the service exits, remove abandoned IPC resources from the service environment:

```bash
easycarla cleanup --instance carla_01
```

Cleanup refuses an active service and is not a process-stop command. See [protocol and ownership](docs/protocol.md).

## Tests, measured coverage and extension points

### Development checks

In the service environment:

```bash
python -m pytest -q
python scripts/check_wheel_install.py --output /tmp/easycarla-wheel-install.json
```

The default suite includes cross-process IPC. Real CARLA tests skip unless explicitly enabled. Wheel checks run outside the source tree in fresh environments; their fake-backend results do not establish simulator correctness.

Real runtime tests need an exclusive CARLA world: **stop any service using that world first**.

```bash
EASYCARLA_REAL_PORT=2000 python -m pytest tests/test_carla_integration.py -q
```

To measure an already running service instead:

```bash
python scripts/measure_service.py --instance carla_01 \
  --episodes 2 --steps 20 --output /tmp/easycarla-timing.json
```

### Interpret validation results

- [Recent correctness patches](docs/testing/correctness-fixes.json): 705 passed / 19 skipped; independent wheels, real Scenic/Bench2Drive physics manifests and rejected-reset paths, plus process/IPC cleanup passed.
- [Parallel measurements](docs/testing/parallel-v1/README.md): same-profile dual Scenic/Town12 step comparisons were about 1.78×/1.65×; mixed sources reached about 1.11× and missed the original target. Two Scenic workers passed a 30-minute fixed-braking run. Four workers were not admitted under the memory budget.
- [Scenario capabilities](docs/scenario-capabilities.md) and [native Bench2Drive evidence](docs/testing/platform-bench2drive-report.md): interface support, short-window execution, mechanism activation, takeover and complete replay are separate validation claims.
- [Map/Vulkan investigation](docs/testing/town13-investigation/README.md): failures remain recorded. Short Town12/13 runs do not establish long-term stability. Full 220-route and all cut-in takeover acceptance cannot be claimed.

These results depend on the machine, configuration, source stage and workload. Recent correctness patches did not rerun throughput measurements or broaden earlier stability claims.

### Repository layout

```text
easycarla/
  runtime.py           Episode lifecycle and decision windows
  simulation/          World settings, sole tick owner, sensors and resources
  scenarios/           Source selection, Scenic events, Bench2Drive adapters
  observations/        Snapshots, histories, geometry, maps and routes
  control/             Ownership, vehicle controls, trajectory/NPC tracking
  tasks.py             Rewards, costs and termination
  extensions/          Materials, public prefixes, diagnostics and replay
  service/             CLI, IPC service, fake backend and fleet
packages/client/       Independent client, protocol, Gymnasium and vectors
configs/               Scenarios, train/eval manifests and fast worker profiles
scenarios/scenic/      Scenic scenario files
examples/              Minimal client demonstration
scripts/               Dependency preparation, measurement and validation
tests/                 Contracts, IPC, lifecycle and opt-in live tests
docs/                  Interfaces, design boundaries and measured evidence
```

Extend the existing responsibilities without importing service dependencies into clients or duplicating ticking/reward logic. `easycarla/envs/gym_env.py` remains a thin local adapter; external projects should use the standalone client. Legacy two-control conversion is available, but versioned observation/reward/termination changes still require migration.

Further reading: [maintenance](docs/maintaining.md), [architecture](docs/audit/architecture.md), [experiments](docs/experiments.md), [sensor synchronization](docs/sensor-synchronization.md), [map load settings](docs/map-performance.md), [Vulkan diagnostics](docs/carla-vulkan.md), [behavior changes](docs/behavior-changes.md).

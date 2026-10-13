# EasyCARLA client

A Linux, same-machine client for an independently started EasyCARLA environment
service. Runtime dependencies are only NumPy and `posix_ipc`; CARLA, Scenic,
Gymnasium and training libraries are not needed in the client environment.

```python
from easycarla_client import CarlaEnvClient

with CarlaEnvClient(instance_name="carla_01") as env:
    obs, info = env.reset(seed=42)
    obs, reward, terminated, truncated, info = env.step([0.0, 0.2, 0.0])
```

Actions are `[steer, throttle, brake]`: positive steer is right, and simultaneous
positive throttle and brake are rejected. Observations are independent copies by
default. Explicit shared views expire on the next request; copy before retaining
history. `close()` closes the session and its episode, leaving the environment
service and CARLA process running.

Protocol v1 uses preallocated POSIX shared memory, NumPy arrays and named
semaphores. A submitted request that times out has an unknown execution outcome;
never automatically replay a `step()`. Server and client protocol versions must
match. The environment repository contains installation, protocol, coordinate
and experiment-extension documentation plus fake-backend and real-CARLA tests.

Install `easycarla-client[gym]` for optional `CarlaGymEnv` and `CarlaVectorEnv`.
The extra adds Gymnasium; the core import still needs only NumPy/POSIX.

```python
import numpy as np
from easycarla_client import CarlaVectorEnv

env = CarlaVectorEnv(["planning_0", "planning_1"], timeout=120)
try:
    obs, infos = env.reset(seed=42)
    env.step_async(np.array([[0, 0, 1], [0, 0, 1]], dtype=np.float32))
    obs, rewards, terminated, truncated, infos = env.step_wait()
    if (terminated | truncated).any():
        obs, infos = env.reset(mask=terminated | truncated)
finally:
    env.close()
```

Numeric observations have fixed shapes published by the service before reset.
Map/route IDs and provenance are in `info`; `get_static()` / `get_route()` fetch
content separately. `action_mode="trajectory"` uses the service's fixed point
count, sampling interval and actor/rear-axle reference. No automatic reset is
performed. `BatchError.completed` retains healthy workers' valid transitions;
failures require explicit reconnect/replacement and reset. Simulator failures
are exceptions, not artificial truncated transitions.

`ClientPool` accepts caller-owned `CarlaEnvClient` instances and offers
`submit(worker_id, "step", action)` / `poll_ready(timeout=...)`. Results retain
completion order and worker IDs; each worker allows one outstanding request.
The pool does not own the clients, replay actions or choose rollout lengths.

# Town13 runtime and large-map loading audit

Date: 2026-09-28. Scope: read existing project code, previous crash evidence,
installed CARLA Python API and upstream CARLA 0.9.15 source. This audit did not
launch CARLA. It does not establish that Town13 has been repaired or that the
map archive is intact. Simulator reproduction and archive checks are separate
parts of this investigation.

## Findings

### 1. Spectator-driven streaming was inherited and unrecorded

Before this change, `WorldSession.load` set no-rendering, synchronous time and
optional streaming distances, but left `spectator_as_ego` inherited. The installed
Python API exposes the property and its default is `True`. The project's effective
world settings and replay recipe omitted it.

CARLA's server applies the property through
`ALargeMapManager::ConsiderSpectatorAsEgo`. When the spectator is the only streaming
reference, setting the property to false removes it. A newly spawned hero becomes
the streaming reference and immediately updates tiles and flushes level streaming
before the spawn returns. Consequently false avoids an unnecessary spectator
reference when no hero exists, while hero-based loading remains active.

Sources: [server RPC](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Server/CarlaServer.cpp#L539-L557),
[large-map actor registration](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/MapGen/LargeMapManager.cpp#L170-L208),
[spectator reference switch](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/MapGen/LargeMapManager.cpp#L1122-L1135).

Implemented a deliberately narrow fix: explicit boolean configuration defaulting
to false, applied before and after loading, read-back checked, recorded in ordinary
info and replay settings. Explicit true remains available. Existing shutdown
restoration is preserved. This is a confirmed configuration/reproducibility gap;
whether it caused the recorded native crashes requires the independent A/B probe.

### 2. Pre-load distances alone cannot eliminate the initial loading window

The project already applies settings before `load_world(reset_settings=False)`
and again after loading. It was incorrect to assume that this preserves reduced
distances throughout the native load operation: CARLA's `NotifyBeginEpisode`
explicitly resets current tile/actor distances to the new LargeMapManager values
before applying settings. The post-load apply is therefore necessary.

Source: [CARLA engine episode initialization](https://github.com/carla-simulator/carla/blob/0.9.15/Unreal/CarlaUE4/Plugins/Carla/Source/Carla/Game/CarlaEngine.cpp#L237-L252).

This produces a period before Python's second apply in which new-map defaults can
be in effect. The current API does not provide an atomic load-with-distances
operation. Disabling the spectator reference before loading is a reasonable
mitigation to test; a timeout increase alone does not remove resource pressure.
Record settings immediately before load, after load and before spawning the hero.

### 3. Initialization frame accounting has an upstream API gap

The CARLA client implementation of `World::ApplySettings` can call `Tick` internally
while checking fixed delta, and `Simulator::LoadEpisode` sends tick cues while
waiting for the new episode. These operations are outside the project's normal
decision loop; they do not imply additional ticks in an ordinary action window.

Sources: [World::ApplySettings](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/World.cpp#L56-L83),
[Simulator::LoadEpisode](https://github.com/carla-simulator/carla/blob/0.9.15/LibCarla/source/carla/client/detail/Simulator.cpp#L87-L113).

However, `WorldSession.load` currently resets its counters after these calls, so
the initialization count covers managed scenario ticks only. Follow-up work
should report API-induced frame deltas separately, with episode IDs and observed
frame bounds. Cross-episode frame arithmetic must not be presented as exact physical
tick accounting without checking CARLA frame-reset behavior. This report does not
change initialization timing or add settling ticks.

### 4. Shutdown restoration can increase load on the last map

`WorldSession` saves the first world's full settings once and applies them to the
current world on shutdown. If the service started from a rendered small map with
default distances and ended on a headless 500 m large map, shutdown can enable
rendering, increase streaming distances and restore spectator streaming after the
hero has been destroyed. `_original_map` is recorded but not used to constrain
this restoration.

That transition follows the existing restoration contract; it is not evidence
that restoration caused the Town13 initialization crash. The earlier Town02
shutdown crash and post-test native shutdown faults make this an important
separate test phase. Do not silently stop restoring external settings. A future
explicit dedicated-server lifecycle mode may leave a quiescent configuration
before an owned server exits; the external-server mode should retain restoration.

### 5. Native ego initialization contains a tile-loading barrier

The pinned Bench2Drive RouteScenario creates a hero with
`CarlaDataProvider.request_new_actor`, whose default `tick=True` advances through
the adapter's initialization path after spawning. RouteScenario then moves the
spectator above the route origin and requests another initialization tick.
The hero spawn itself may already have synchronously flushed its surrounding
tiles in native code. A log stopping between `LOADED` and `PREPARED` therefore
does not identify actor spawning, scenario trees or spectator positioning alone.

The minimal probe should persist phase markers before/after hero spawn, first
tick, spectator relocation, second tick and cleanup. Avoid adding unrelated
traffic or sensors until these phases have repeated successfully.

### 6. Current crash logs do not prove missing map files or out-of-memory

The Town13 records show SIGSEGV and a render-thread timeout, without an asset path
or missing-package error. A prior Town13 route 3816 ran for 1,500 ticks, so the map
is not universally unloadable. That successful run does not prove every tile is
intact. The related Town12 crash stack points into Vulkan uniform-buffer allocation
while adding scene primitives; its crash context did not flag system OOM.

Evidence: [Town13 repeat crash](../platform-town13-repeat-crash.txt),
[Town13 renderer timeout](../platform-town13-render-thread-crash.txt),
[Town13 limited successful run](../platform-bench2drive-highway-smalltiles.json),
[Town12 native stack](../platform-matrix-town12-native-diagnostics.txt).

## Explicit map-performance profiles: follow-up design

Resolve overrides by the selected map before loading. Keep map profiles small and
declarative: spectator reference, rendering mode, tile distance and actor active
distance. Merge each map's profile against the immutable base configuration,
never the preceding map's resolved settings. Preserve the exact map asset name;
do not substitute `_Opt` variants silently.

Record requested profile name/hash, exact resolved map, effective settings,
server/API versions, renderer startup flags and route identity in test output.
Render options and sensor changes must be explicit. The current 500 m distances
are a diagnostic configuration, not a generally validated official benchmark
profile. Bench2Drive builds nearby scenarios within 500 m, so making distances
smaller without checking actors and routes can change experiment behavior.

Use a rendered low-quality profile when cameras are required; no-rendering mode
is only suitable for configurations without RGB/BEV cameras. Measure GPU memory,
process RSS, load time and physical-tick p50/p95 separately. Changing tile/actor
distance can alter dormant actors and traffic. Prove scenario activation and
completion again for each accepted profile.

`MapLayer.NONE` is an explicit request, not proof that Town12/Town13's content was
removed. CARLA documents layer selection for optimized maps; large-map tile
streaming is a separate mechanism. Do not claim a measured memory benefit from
the layer flag without observing it. See [large maps](https://carla.readthedocs.io/en/0.9.15/large_map_overview/)
and [Python API map-layer semantics](https://carla.readthedocs.io/en/0.9.15/python_api/#carlamaplayer).

## Checks performed for the narrow configuration fix

Command:

```bash
/home/czh/software/miniconda3/envs/CARLA-RL-Base/bin/python -m pytest \
  tests/test_world_loading.py tests/test_config.py \
  tests/test_runtime_experiments.py tests/test_runtime_core.py -q
```

Result: **281 passed in 0.44 s**. Coverage includes default/explicit spectator
configuration, strict boolean validation, pre/post-load application, read-back
refusal, full original-settings restoration and replay preflight mismatch.
Real CARLA A/B and full-map stability acceptance were **not run by this audit**.

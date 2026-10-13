# Behavior changes in 0.2

This is an explicit protocol migration, not an assertion of numerical equivalence
to the old monolithic environment. Baseline source hashes are in audit/baseline.json.

* Main actions are `[steer, throttle, brake]`, positive steer right. Invalid actions
  are rejected before simulation. The deprecated in-process Gym shim converts old
  `[steer, signed_acceleration]` and rejects implicit ego autopilot/external control.
* Observations are named SI-valued arrays in ego FLU (or explicitly world RH), with
  timestamps, masks, IDs and unnormalised history. Angular velocity is radians/s;
  accelerations are rotated; LiDAR is converted from sensor coordinates once.
* Default physics/decisions/history are 20/5/10 Hz. Legacy Scenic `*_steps` event
  durations are interpreted at their old 0.1 s scale; per-step crossing probability
  is converted to physical ticks. Initialization/replay ticks are separately counted.
* Old `_get_cost` existed but was never returned. Results now include cost and
  per-component costs. Time-limits no longer overwrite natural termination.
* Reward `driving` uses route progress (0.1/m), time (-0.1/s), normalised acceleration
  (-0.1/s), jerk (-0.05/s), overspeed (-1/s), collision (-50), offroad (-10), signal
  violation (-10), and goal (+10). All weights are configurable. Events are counted
  once by identity. This replaces old speed/waypoint reward; it is not the old formula.
* Route completion and reward share the same event. A fixed time limit truncates;
  normal terminal events take precedence. No autoreset; subsequent step requires reset.
* A lost actor, bad simulator tick or failed required sensor faults the episode,
  rather than returning stale/zero observations and a synthetic collision reward.
* Reset can rebuild its exclusively leased world. Actor cleanup uses ownership;
  close never kills externally started CARLA. World settings are restored on service
  shutdown, not on every client disconnection.
* Scenic route resampling is explicit, same-source and recorded; a failed sampled
  spawn is not replaced silently. Cross-source fallback defaults to disabled.
* `allow_skip_if_incompatible=false` now raises. Event failure stops the controlled
  vehicle; lead-brake abort restores its original TM control. Completion uses the
  configured TM port.
* Bench2Drive native evaluation and RL use distinct task profiles and metadata.
  Truth observations are privileged input, not a sensor-leaderboard submission.

The old environment implementation and parameter-only provider have been replaced;
the old import paths remain small migration shims, not parallel business logic.

The former reward is frozen in `tests/fixtures/legacy_reward_v0.py`, with golden
cruise, stop, collision and arrival cases. This reference is never used by the
runtime and does not preserve the old incorrect goal-radius/coordinate behavior.
The new jerk component differentiates acceleration in inertial world RH axes,
so rotating the ego observation frame alone does not create jerk.

Scenic route rejection sampling now has a random stream independent of background spawning. A recipe skips rejected routes without shifting background blueprint/order sampling. World ownership locks reserve the CARLA port across host aliases (same-machine v1); localhost and 127.0.0.1 cannot claim different owners for the same port.

## Hardening changes

* Actor controls are now committed through acknowledged batches before physics.
  Same-owner setters within one tick coalesce; changing owners requires a handoff.
  This corrects a timing-sensitive asynchronous submission path and can change
  exact trajectories. It does not change action scales or reward weights.
* Observations, reactive control and traffic-rule checks use one captured physical
  frame. Static maps are cached by content digest with bounded memory retention.
* Freshly spawned actors are cleaned even when CARLA's previous snapshot reports
  `is_alive=False`. Cleanup of other components continues after one cleanup fails;
  the aggregate failure is reported.
* `simulation.no_rendering_mode` and `map_layers` are explicit. Camera RGB/BEV is
  rejected with no-rendering mode. Short town names resolve to exact installed
  asset paths and the loaded town is verified. No automatic town substitution is
  introduced. Headless/layers-none validation is distinct from rendered/full-map
  benchmark fidelity.
* Diagnostic episode recipes are version 2; old version 1 materials are rejected
  before world reconstruction. JSON/array communication protocol remains version 1.
* CLI asset paths resolve relative to the config file, or its explicit `path_base`.
  Repository configs declare `path_base: ..`; launching them from another working
  directory now has the same result. Direct Python dictionaries retain normal
  caller-relative path semantics.
* Timings separate control commit, physics, sensor waiting and observation build.
  `simulation_seconds` no longer includes the control-commit barrier; comparisons
  with older performance reports should use the full environment time as well.

* Event `max_active` now caps actual event instances even inside global fallback
  placement. Construction props belonging to one event do not each consume a
  separate event slot. Requested/spawned/skipped counters share the instance unit;
  `long_tail_specs_requested` separately records selected specifications.
* Bench2Drive newborn traffic initialization can explicitly disable TM for a
  one-tick velocity seed and restore it on the next frame, or hand over permanently
  to its native constant-velocity source. External takeover leases are respected.
  These adapter semantics are recorded and are not bitwise upstream equivalence.
* Large-map streaming and actor activation distances are optional positive
  settings. Defaults preserve CARLA's values; reduced distances are explicit in
  info/recipe and must not be presented as full-map benchmark fidelity.
* `simulation.spectator_as_ego` now defaults to `false` instead of inheriting
  CARLA's `true`. Large-map tiles follow the real hero; during loading or between
  episodes an empty world does not stream tiles around the spectator. Set it to
  `true` explicitly for spectator-only exploration. The setting is applied both
  before and after map loading, verified by read-back, recorded in info/recipes,
  and restored with the original world settings on service shutdown. Existing
  prefix materials lacking this field must be recorded again; the change is not
  a verified cure for every native renderer crash.
* Scenic's CARLA ego blueprint now uses `role_name=hero`. The previous value
  `ego` is not recognized by CARLA's large-map manager and cannot reliably keep
  tiles loaded when spectator-driven streaming is disabled. The internal role
  remains `scenic/ego/1`, preserving semantic observation and research identities.
  Only the engine-facing actor attribute changes; regenerated initial manifests
  record it explicitly. This corrects the native loading contract rather than
  changing Scenic's route or background sampling.

* Each WorldSession performs an initial world load to honor its requested map
  layers, even with reload_on_reset=false. Subsequent same-map/same-layer resets
  can reuse the world. A single load may be followed by bounded, read-only map
  metadata polling; it never retries loading or adds physics ticks. Effective
  CARLA world settings are recorded alongside requested values and checked in
  reconstruction, including inherited streaming defaults.
* A trajectory rejected by a full prefix recorder leaves both physics and the
  tracker state unchanged; capacity is checked before starting the tracker.
* `simulation.map_overrides` optionally sets streaming/activation distances for
  exact map names. Each load merges from unchanged base values and reads the new
  world's settings before applying overrides. `None` follows actual server
  settings; explicit base values are required for guaranteed cross-map distance
  restoration. Requested overrides and actual settings are recorded in info and
  recipes; old prefix materials without these fields require recording again.
  See [map performance configuration](map-performance.md) for fidelity limits.

* CARLA launch examples and the owned diagnostic launcher now default to `Epic`.
  On the audited machine, `Low` reproducibly crashes Town03 in native Vulkan
  pipeline creation, while the Epic map/RGB matrix passes. This is an explicit
  rendering-preset change, including image appearance; record the external
  simulator command with experiment data. It is not a binary patch or proof
  that every native crash shares the same cause. Low remains an explicit
  diagnostic choice, not an automatic fallback.
* `configs/bench2drive_eval.yaml` now fixes both streaming and activation
  distances at 650 m, with rendering, all layers, spectator streaming disabled
  and a 90 s RPC timeout. Previously these distances inherited server state.
  The separate `bench2drive_low_load.yaml` demonstrates per-map overrides;
  its selective configuration must not be confused with the global-distance
  map/RGB test. Activation distances affect distant NPC dormancy, so this
  configuration change must be retained in episode and experiment metadata.
* Native route and matrix validation now reload the CARLA world for each route
  by default, matching `EnvironmentRuntime` and the pinned Bench2Drive evaluator.
  Previously the route CLI silently selected `reload_on_reset=false`, and the
  matrix inherited that behavior. `--reload` remains accepted; `--no-reload`
  explicitly selects same-world reuse for diagnosis, and the matrix propagates
  and records the choice. Both evaluation configs state `reload_on_reset: true`.
  This corrects an acceptance-policy mismatch; it is not a native crash fix or
  proof that long batches are stable. It reloads the world without restarting
  CARLA or replaying an uncertain action.
* World loading now waits for the returned episode's `on_tick` initialization
  callback before reading map metadata. CARLA 0.9.15 can return `load_world`
  before its synchronous Traffic Manager map rebuild has completed; the new
  barrier uses the existing initialization notification and never sends an
  additional tick or retries the load. It is bounded by the simulation timeout,
  rejects an unchanged episode ID and removes its own callback on every exit.
  Ordinary `step()` timing and TM ownership are unchanged. This addresses a
  verified unsafe ordering associated with a client `get_map()` SIGSEGV;
  real cross-map validation is required separately from unit tests. See the
  [source audit](testing/town13-investigation/client-tm-readonly.md).
  A readiness timeout is now a typed `WorldInitializationTimeout`; native batch
  validation stops with an unknown initialization outcome and leaves subsequent
  routes unrun, rather than attempting another load while TM may still reset.
* The minimal external-client example exposes `--timeout` and defaults it to
  180 seconds for large-map resets. The library default remains 30 seconds;
  neither setting enables replay of an uncertain request.

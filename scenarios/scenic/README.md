# Scenic Templates for CARLA-RL-Base

These templates are consumed by the executable `ScenicAdapter` in
`easycarla/scenarios/scenic_source.py`. The former provider import is a migration shim.

## How It Works

The adapter samples Scenic parameters, owns actor creation and runs modular events on each physics tick:

- `weather` -> CARLA weather preset (for example `ClearNoon`)
- `traffic_light_policy` -> `"on"` or `"off"`
- `ego_spawn_index` -> ego spawn point index from CARLA map spawn points
- `destination_spawn_index` -> route destination spawn point index
- `min_route_distance` -> minimum route distance threshold in `reset`
- `number_of_vehicles` / `number_of_walkers` -> NPC counts for this episode
- `forced_crossing_walkers` -> force a subset of walkers to spawn in front of ego
- `forced_crossing_distance_min/max` -> front-distance window (meters) for forced crossing walkers
- `forced_crossing_lateral_min/max` -> side offset window (meters) from ego lane center
- `forced_crossing_trigger_prob` -> per-step trigger probability for forced crossing during driving
- `forced_crossing_cooldown_steps` -> minimum step interval between forced crossing triggers
- `forced_crossing_warmup_steps` -> startup steps before forced crossing can trigger
- `forced_crossing_min_speed` -> ego speed threshold (m/s) required to trigger forced crossing
- `forced_crossing_max_total` -> per-episode cap on total forced-crossing walkers spawned
- `hard_break_route_enabled` -> spawn a dedicated lead vehicle for the HardBreakRoute scenario
- `hard_break_lead_spawn_distance_min/max` -> front-distance window for the dedicated lead vehicle
- `lead_brake_*` -> trigger window, ego-speed threshold, braking strength, hold duration, and per-episode cap for lead braking
- `long_tail_event_type` -> one of `parking_cut_in`, `vehicle_opens_door`, `construction_obstacle`, `mixed_long_tail`
- `event_distance_min/max` -> route-distance window used to choose the event anchor
- `trigger_distance_min/max` and `reaction_time` -> trigger rules for interactive vehicle events
- `direction` -> `"left"`, `"right"`, or `"auto"` for side-dependent events
- `allow_skip_if_incompatible` -> skip unsupported map/road geometry without failing `reset`

## Map Compatibility

Bench2Drive places side-dependent events at hand-picked route trigger points.
Scenic uses parameterized routes with recorded same-source resampling, so runtime compatibility checks decide
whether a sampled route can support an event:

- `parking_cut_in` first tries route-side lanes, then falls back to map-wide roadside parking candidates.
- `vehicle_opens_door` requires a usable side lane and a vehicle blueprint with
  dynamic doors.
- `construction_obstacle` first tries clear route geometry ahead, then falls
  back to several map-wide straight-road anchors when enabled.

When `allow_skip_if_incompatible` is true, an incompatible event is skipped and the reason is reported in
`info` as `long_tail_skip_reason`.

## Templates

- `pedestrian_crossing.scenic`: forced pedestrian crossing and high walker activity.
- `hard_break_route.scenic`: dedicated lead vehicle braking hard in front of ego.
- `parking_cut_in.scenic`: parked/side-lane vehicle cuts into ego lane.
- `vehicle_opens_door.scenic`: parked vehicle opens a door into ego's path.
- `construction_obstacle.scenic`: construction cones/sign/debris in ego lane.
- `mixed_long_tail.scenic`: samples from the new event families and relies on
  compatibility checks to select feasible events.

## Enable Scenic Backend

Example environment params:

```python
params = {
    "scenario_backend": "scenic",
    "scenic_file": "scenarios/scenic/mixed_long_tail.scenic",
    "scenic_max_iterations": 2000,
    "scenic_mode2d": False,
    "long_tail_event_max_active": 1,
}
```

If Scenic is missing or sampling fails, provider falls back to random sampling
and reports `scenario_backend="scenic_fallback"` in `reset()` info.

If skipping is disabled, incompatible geometry fails reset. Cross-source fallback is separately configured and disabled by default. These Scenic event templates are not the native Bench2Drive implementation; native routes use the independent Bench2Drive adapter.

Legacy event duration parameters named `*_steps` retain their 0.1-second reference scale and are converted to physical ticks. `forced_crossing_trigger_prob` is likewise converted from its old 10 Hz probability.

# Scenic event components

`LongTailEventManager` selects configurations and dispatches independent `ParkingEvent`, `DoorEvent`, and `ConstructionEvent` objects. There is one implementation of each family. `CrossingEvent` and `LeadBrakeEvent` own explicit configuration/state dataclasses. None of the event classes forwards unknown attributes to the environment.

A prepared event follows `EventLifecycle`: `before_tick()`, `after_tick()`, `close()`, `info()`, and `export_state(actor_roles)`. Construction/preparation accepts family-specific parameters. The manager's `prepare(specs)` creates bounded components. `ScriptedEvent` supplies resource ownership, event outcome accounting, and the shared mechanics for static-location events; `EventGeometry` supplies explicit geometry/sampling helpers. Adding a family requires a component and a manager registration, not another manager superclass.

All actuator writes use `EventContext.control`, `set_autopilot`, or `set_target_velocity`. They identify the owner as `scenic/event/<family>`. `session.controls` checks ownership and commits the commands before the next physics tick. A lead-brake takeover explicitly hands ownership over before disabling the previous Traffic Manager source. `ControlConflict` always propagates: it must not be counted as a geometric incompatibility or normal event failure.

`max_active` is a hard event-instance capacity at preparation, including explicitly enabled global fallbacks. Construction props within one obstruction are one event instance, regardless of how many props it contains. `long_tail_event_requested`, `long_tail_event_spawned`, and `long_tail_event_skipped` count instances; `long_tail_specs_requested` counts selected configuration records. Partial fallback placement records the missing instances with `global_fallback_partial_spawn`. This corrects the previous behavior where a single fallback could exceed the manager's capacity.

A geometry incompatibility returns `False`, records the reason, and obeys `allow_skip_if_incompatible`. Unknown event types and disallowed towns obey that same setting. Unexpected preparation exceptions propagate after owned-actor cleanup. An API or ownership failure does not silently become an optional skip.

State exports have `schema_version: 1`, declared config/state fields, PRNG state, semantic actor references, and explicit limitations. No Python object attribute enumeration participates in the export. Adding a diagnostic cache therefore does not change saved execution material. Changing the declared state contract requires a schema review/version change. Scripted event geometry retains CARLA's native world axes (x/y/z in meters); it is diagnostic data, separate from policy geometry in right-handed world/ego coordinates. Missing actor roles are explicitly unresolved, never substituted with process-local CARLA actor IDs.

State export supports rebuild/replay comparisons; it is not full simulator-state restoration. Traffic Manager state and physics hidden state remain outside this contract. Unit tests in `tests/test_events_contracts.py` verify state-machine outcomes, cleanup, mandatory failures, semantic references, capacity, and ownership propagation.

# Physical-frame observations

`ObservationBuilder.sample(snapshot, tick)` captures a committed physical frame once. `WorldSnapshot.find(actor.id)` supplies kinematics; actuator/signal telemetry is read once while the synchronous world is stopped. `frame_state.actors` and `get_state(actor_or_id, frame=...)` expose immutable inputs for control/task consumers. Repeated `build` or same-frame sampling does not read actors again or append duplicate history. Missing ego state, backward frames, and frame mismatches are faults.

Captured mappings and sequence values are immutable. Public observation arrays remain independently assembled. Static geometry uses a process-local LRU keyed by OpenDRIVE/schema digest, bounded to four maps and 256 MiB of array storage by default. Its arrays have immutable byte backing; metadata dictionaries are detached for each caller. Map aliases sharing identical OpenDRIVE reuse geometry while retaining their requested map name.

`tests/test_observations.py` checks frame reuse, snapshot kinematics, history compensation, immutability, content caching, and both LRU limits. `tests/test_control_gateway.py` checks commit-before-tick ordering, copied staged arguments, TM/constant-velocity handoffs, partial-batch faults, and registry cleanup.

# Real event evidence

`python scripts/summarize_scenic_coverage.py docs/testing/hardening --require-complete` audits generation, per-instance trigger coverage, completion, and explicit skips independently from `execution_passed`. A successful loop with no event activity cannot pass. A collision can be a valid environment termination while leaving event completion unverified.

`validate_scenic_completion.py` supplies a small explicit test driver: it brakes after door/lead-brake triggers, and follows an adjacent-lane bypass for construction when the map permits it. It records its configuration, route strategy, all submitted actions, final event counters, and any execution/cleanup error. It does not teleport actors, alter physics, or modify production event behavior.

# Migration audit and boundaries

The baseline is the user's **working tree**, not only HEAD. `baseline.json` records
its hashes, original status and the external backup archive. No existing local
Bench2Drive checkout is edited by this refactor.

The previous environment had 2654 lines, the provider 753 and the event manager
1169. The module named `carla_env_test.py` was production code, not a test suite.
`main.py` was empty. There was no training loop or formal automated test suite.

Preserve: five long-tail event families, Scenic parameter sampling, random route
selection, optional RGB/semantic BEV/LiDAR, direct continuous vehicle control,
weather/background traffic, route progress and outcome diagnostics.

Correct explicitly: asynchronous latest-frame observations; destruction of all
world vehicles; ego loss disguised as collision; time limit overriding collision;
inconsistent goal radii; disconnected episode seeds; implicit Scenic fallback;
unused safety cost; mixed coordinate units; hidden simulation ticks.

Migration: world/tick/owned actors -> simulation; lifecycle -> runtime;
providers/events -> executable scenarios; actions/controllers -> control;
history/map/sensors -> observations; reward/cost/outcomes -> tasks;
session/request transport -> service + separately installable client;
prefix/diagnostics/interventions -> optional extensions.

Dependency direction: client(protocol) <- service -> runtime -> simulator and
scenario adapters. Controllers, tasks and replay comparison operate on plain
numeric data. No training code or eight-branch scheduler belongs here.

Baseline test command:
`python -m pytest tests/test_legacy_characterization.py -q`.
These tests characterize template compilation and existing event families; they
do not prove that an event is reachable or correct in a live simulator.

## Platform hardening

The runtime now depends on `scenarios.contracts.EpisodeProtocol`. Every executable
source supplies lifecycle methods, supported task profiles, control resolution,
task facts and a versioned diagnostic state. The decision loop does not dispatch
by Scenic/Bench2Drive names or probe for optional controller attributes. A new
source implements this contract and a selector adapter; it does not extend the
runtime with source-specific fields.

All actuator writes are staged through `control.gateway.ControlGateway`.
The gateway owns control leases and coalesces repeated setters from the same
source. A different source requires an explicit handoff. `WorldSession.tick`
flushes acknowledged batches with `do_tick=False` before advancing physics.
Constant-velocity RPCs have a separate ordering barrier, since CARLA has no
corresponding batch command. Failed commits fault the episode before its next
tick. Registry removal also removes queued commands and control ownership.

Scenic events use composition: an event manager selects independent parking,
door and construction components; crossing and lead-brake components follow the
same lifecycle. Event geometry is a shared service rather than inherited hidden
methods. Component state exports declare their schema and limitations.

`observations.frame.FrameState` freezes one physical snapshot. Observation
history, ego trajectory tracking, reactive NPC decisions and traffic-rule events
consume this same captured data. Signal associations and actors are enumerated
once per frame. Static map geometry has a content-addressed LRU cache bounded by
both entry count and byte count; its arrays cannot be made writable by callers.

`extensions.session.ExperimentSession` owns prefix recording, portable materials
and diagnostic identities behind an explicit runtime port. It never injects
private runtime fields or scrapes arbitrary Python object attributes. Rebuilding
uses a source-config argument and does not overwrite the service's configured
mixed-source policy. Recipe v2 rejects old implicit-state recipes before mutation.
Reset, client-session close and service shutdown have distinct cleanup scopes.

The installation boundary is tested with **built wheels in fresh, independent
Python environments**, in addition to development-tree tests. Linux CI separates
core protocol tests, Scenic compilation tests and wheel installation/transport
tests. Real CARLA validation remains opt-in and produces separate evidence.

There are still external integration limits: native CARLA/Bench2Drive physics and
hidden controller state are not snapshots; an upstream crash cannot be repaired
by the Python gateway. Repeatability and scene-role capabilities must be supported
by measured evidence. See the latest [hardening report](../testing/hardening/README.md).

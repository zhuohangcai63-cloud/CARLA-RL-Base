# Pure CARLA map diagnostic

`scripts/diagnose_carla_maps.py` connects to an already running, dedicated local
CARLA process. It imports CARLA and NumPy but no environment, Scenic or Bench2Drive
implementation. **It reloads maps and therefore destroys the existing world's
actors. Do not point it at an active experiment or shared simulator.** The port
must be supplied explicitly, and its world lease excludes other easycarla owners.
Startup readiness uses fresh CARLA connections with read timeouts of at most one
second until the configured total timeout. These retries only read server version
and world state; they never retry a mutation or advance physics.

Run from the repository in the CARLA service Conda environment. This example
performs three consecutive loads of each map, with a stationary hero and exactly
100 explicitly requested ticks per loaded world:

```bash
python scripts/diagnose_carla_maps.py --port 2600 \
  --town Town12 Town13 --repeats 3 --ticks 100 \
  --distance 500 --no-spectator-as-ego --no-rendering \
  --output /tmp/carla-large-map-probe.json
```

All 12 Bench2Drive map names can be supplied after `--town`. Default spawning uses
the map's first recommended spawn. `--spawn=x,y,z,yaw` instead uses exact CARLA
world coordinates (metres and degrees), without snapping to another road.

For a known route origin, supply a manifest, route ID and checkout root. Only the
XML is read; its SHA-256 must match the manifest. The hero starts at the first route
point plus 0.5 m in height, oriented along its first segment:

```bash
python scripts/diagnose_carla_maps.py --port 2600 \
  --route-manifest configs/manifests/v004_eval220.json \
  --route-id 34183 --route-root "$BENCH2DRIVE_ROOT" \
  --repeats 3 --ticks 100 --distance 500 --no-rendering \
  --output /tmp/carla-town13-route-origin.json
```

This is **not** route or scenario execution. It does not build native traffic,
behaviour trees or benchmark criteria. To compare the spectator effect, repeat a
controlled run with `--spectator-as-ego`; omit it or use
`--no-spectator-as-ego` for false. Use a fresh server for independent comparisons.
The script never launches or restarts the server itself.

Add `--camera` and omit `--no-rendering` to check an RGB camera. Every requested
physical frame must deliver 320×180 BGRA bytes with nonuniform RGB pixels (standard
deviation at least 1). It never advances additional ticks to catch up a camera.
An offscreen server can still render cameras; engine-level null-renderer modes
cannot validate RGB. Camera pixels are inspected but not saved.

The JSON report is atomically persisted before each potentially failing stage.
It records map and OpenDRIVE identity, original/loaded/effective settings,
same-episode frame changes around native initialization APIs, hero/camera setup,
an idle-frame check, contiguous action ticks and physical time, pixel checks,
tick/camera-wait p50/p95/max, and cleanup. `MapLayer.All` is always requested.
Readiness attempt count and elapsed time are retained. Per-tick state is persisted
to the report while console output is limited to setup and cleanup phases.
Distances of 500 m are explicitly reduced streaming/activation settings and
must not be represented as official benchmark equivalence.

The exit status is zero only if execution and restoration both pass. A failed
tick is not retried. When possible the script destroys only its own actors and
restores the original settings; an externally changed world or unresponsive
server is reported as a cleanup failure. A report write failure makes the command
fail but does not prevent owned-actor cleanup or original-settings restoration.
Restoring original rendering/streaming
settings can increase load, so cleanup has its own durable phase marker. A native
client abort or forced process exit can leave the last report marked `running`;
the supervising process must classify that attempt as incomplete/unknown, never
as passed. The script does not claim that finite successful samples prove all
routes or long-term stability.

No-CARLA verification: `python -m pytest tests/test_diagnose_carla_maps.py -q`
passed **14 tests** during development. These fake checks do not replace real
simulator acceptance.

The separate Scenic regression exercises the real environment after the pure API
probe. It checks native hero registration, explicit 650 m map overrides, a stable
idle clock, 15 braking decisions without falling through the road, owned cleanup
and restoration of world settings:

```bash
EASYCARLA_LARGE_MAP_PORT=2600 \
EASYCARLA_LARGE_MAP_REPORT_DIR=/tmp/scenic-large-map-check \
python -m pytest tests/test_large_map_integration.py -q -s
```

This requires an exclusively available CARLA server and loads Town12 and Town13.
`EASYCARLA_REAL_PORT` alone intentionally does not enable these two tests. Without
the dedicated variable they are skipped, not reported as simulator passes.

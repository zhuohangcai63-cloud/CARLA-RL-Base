# Scenic event coverage in the architecture hardening run

`scenic-coverage.json` separates program execution from actual event generation, triggering, completion, and explicit skips. Regenerate it with:

```bash
python scripts/summarize_scenic_coverage.py docs/testing/hardening --require-complete
```

The strict command currently returns nonzero. The evidence is:

| Family | Baseline real episodes | Additional completion probe | Remaining limit |
|---|---|---|---|
| Pedestrian crossing | Both seeds generated and triggered two walkers. Seed 42 completed one; the other three walkers timed out. | None needed to demonstrate the completion path. | Trigger coverage is complete for these instances; completion rate is 1/4. Timeout geometry/physical obstructions are not hidden. |
| Lead braking | Both seeds spawned a dedicated lead. Seed 42 triggered and ended in ego collision; seed 43 did not trigger within the 120-decision driver budget. | Seed 42 brakes ego after the trigger, waits for the configured hold, and verifies lead completion at decision 141. | Seed 43 trigger remains unverified under the baseline driver. |
| Parking cut-in | After the hard-cap fix, each seed spawns one event and triggers it. Seed 42 demonstrates completion. | Not required for the observed completion path. | Results are limited to the tested routes/seeds, not all parking geometry. |
| Opening a door | Seed 42 spawned/triggered and ended in ego collision; seed 43 explicitly skipped with `no_side_lane`. | Seed 42 brakes ego after opening and verifies hold completion at decision 55. | The seed 43 route cannot support this event's required side lane. |
| Construction obstruction | Both seeds spawned and triggered one event; the simple route-following driver collided with the obstruction. | A fixed one-lane offset still collided in seeds 42/43. A seed 42 bypass sized from actual prop bounding boxes plus ego width and 0.7 m clearance completed successfully. | The test driver must account for actual obstacle extent; a nominal lane-center shift is insufficient. Failed probes are retained. |

The lead/door probes change only the external validation driver's actions. The construction probe creates a test-driver route while leaving the environment's configured scenario route and obstacle placement unchanged. No test teleports actors, disables collisions, or substitutes an event counter for observed completion.

The budget fix was retested in a new process for parking and construction. Both fallback generators now obey the remaining event-instance capacity before spawning. All four repeated episodes ran successfully and report one requested/spawned event per episode; a construction event may contain multiple props. The original run before that fix allowed one parking fallback to exceed `max_active=1`; that result is not used as post-fix capacity evidence.

The successful construction probe is retained in `construction-bounds42/carla-completion-construction_obstacle.json`, including its complete external test route, action sequence, final physical state and event counters. The selected bypass position was checked against a CARLA driving lane. The environment's event placement, original route, collision/termination semantics, and actor physics were unchanged. The earlier failed probes remain in the top-level completion report and `construction-seed43/`.

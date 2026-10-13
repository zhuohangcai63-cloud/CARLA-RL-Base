# CrossingBicycleFlow route 35418: bounded diagnosis

The unchanged pinned upstream scenario and route input cannot construct their bicycle flow on the OpenDRIVE content used by this run. The selected biking lane has no forward connection into its next junction. This was reproduced with `carla.Map("Town12", local_xodr_text)` using CARLA 0.9.15; no client, world load, actor command, settings change, or tick was used by the offline audit. No source or route was changed.

The separate [read-only server map observation](platform-town12-readonly-map.json) confirms that RPC 2100's active Town12, the installed asset, and the local cached asset have the same OpenDRIVE SHA256: `26fa53d4902ac3397d35f948969f0c77c1bf023755ede2c67edf00bef817151f`. Thus the offline reproduction uses the actual run's map content. It reproduces the construction failure, not a whole episode or simulator execution.

## Input and source comparison

The run used pinned commit `acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8`, preparation patch p3, seed 123, exact map `Carla/Maps/Town12/Town12`, no rendering, map layers `none`, and streaming/active distances of 500 m. The [run log](platform-bench2drive-matrix220-headless-Town12.log) lines 294–296 and [child report](platform-bench2drive-matrix220-headless-Town12.json) record initialization failure before any decision tick, with zero resources left in the owned registry.

Compared the p3 dependency files directly against `git show <pinned_commit>:<path>`:

| File | Result | SHA256 |
| --- | --- | --- |
| `scenario_runner/srunner/scenarios/cross_bicycle_flow.py` | Byte-for-byte identical | `1c7714588fb1f44356cf1595087c6aaf27db25ac96b65275c8c59065c8966a31` |
| `leaderboard/data/bench2drive_0.0.4_val.xml` | Byte-for-byte identical; also matches manifest digest | `885debbc3d87f14351b521d8da0dc9d25fcee1f0d926ed1c92cc9ff62504ca45` |

The manifest entry is [v004_eval220.json:1036](../../configs/manifests/v004_eval220.json#L1036). In the p3 XML, route 35418 begins at line 4990; its scenario parameters are at lines 5020–5025:

| Parameter | Exact XML value |
| --- | --- |
| Route | `id=35418`, `town=Town12`, `road_id=675` |
| Scenario | `CrossingBicycleFlow_1`, type `CrossingBicycleFlow` |
| Trigger | `x=912.7, y=3928.9, z=370.0, yaw=189.2` |
| `start_actor_flow` | `x=853.9, y=3928.4, z=370.5` |
| `end_actor_flow` | `x=904.3, y=3936.4, z=370.1` |
| `flow_speed` | `11` |
| `source_dist_interval` | `from=14, to=26` |

The parser's only p3 change is `scenario.getchildren()` → `list(scenario)` at `leaderboard/leaderboard/utils/route_parser.py:132`; lines 133–138 preserve trigger coordinates and other parameter dictionaries. The adapter passes the selected parsed configuration to `RouteScenario`; `WorldBridge` forwards `get_map`, and `CarlaDataProvider` stores that native map (`carla_data_provider.py:213,227–240`). No adapter path changes this scenario's biking-lane projection or successor selection; this code does not use the cached global route planner.

## Exact failing path

In unchanged `cross_bicycle_flow.py`, line 111 projects `start_actor_flow` onto `LaneType.Biking`; lines 112–113 reject projections over 10 m away. Lines 119–124 repeatedly choose `wp.next(2)[0]`. Completion requires entering a junction, leaving it, then getting more than 40 m from the exit (lines 126–132; fixed distance at line 73). **The XML `end_actor_flow` is never read by this upstream class.** Flow speed and source interval do not affect this path construction.

The [complete offline trace](platform-crossing-bicycle-35418-trace.json) used a 2,000-iteration bound and stopped after six successor queries:

| Query index | Road / section / lane | `s` (m) | Successors | Phase |
| --- | --- | --- | --- | --- |
| 0 | `264 / 0 / -3` | 62.944992 | 1; choose index 0 | 0, before junction |
| 1 | `264 / 0 / -3` | 64.944992 | 1; choose index 0 | 0 |
| 2 | `264 / 0 / -3` | 66.944992 | 1; choose index 0 | 0 |
| 3 | `264 / 0 / -3` | 68.944992 | 1; choose index 0 | 0 |
| 4 | `264 / 0 / -3` | 70.944992 | 1; choose index 0 | 0 |
| 5 | `264 / 0 / -3` | 72.944992 | **0** | 0 |

Source projection is `(853.809143, 3928.404541, 370.485016)`, only **0.092225 m** from the supplied source. The final waypoint is `(863.694519, 3929.914551, 370.418091)`. Every sampled waypoint is outside a junction; there are no branches, no junction-entry transition, and no exit location. The sixth query necessarily reaches the upstream `ValueError` at line 121. It fails before the 40 m exit requirement becomes relevant.

Static XML corroborates the trace in `/home/czh/software/carla/CarlaUE4/Content/Carla/Maps/Town12/OpenDrive/Town12.xodr`:

- Line 38805: road 264 is 74.05 m long; line 38808 gives successor junction 14055.
- Lines 38918–38926: lane `-3` is a biking lane.
- Junction 14055 begins at line 886334. Its six connections with `incomingRoad="264"` are at lines 886335, 886344, 886347, 886354, 886360, and 886364. Their incoming lane IDs are `-2`, `-1`, `6/5`, `-1`, `-5/-6`, and `-2`; none connects lane `-3`.

## Classification and limits

This is a reproducible incompatibility between the fixed upstream bicycle-flow path construction/input and the verified Town12 topology. The evidence excludes adapter parameter changes, planner-cache route choice, and an ambiguous `next()[0]` branch at this failure. It does not establish whether another CARLA/Town12 release supplies a different bicycle connection or what route alteration upstream intended.

The preparation patch changes the handling of the error: upstream `route_scenario.py:341–347` prints a warning and skips the failed scenario; p3 lines 343–344 raise it instead. The patch exposes the construction failure; it does not cause the empty successor list. A successful upstream route run that silently skips this scenario would not demonstrate successful bicycle-flow execution.

Do not silently substitute a driving lane, shorten the flow, or use the ignored endpoint as a workaround. Any such change needs an explicit route/map compatibility decision. Routes **35907** and **35623** in the same XML contain the same bicycle source coordinates and class; their identical source-plan stage is subject to the same topology failure, although this audit did not execute those episodes.

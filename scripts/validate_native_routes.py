#!/usr/bin/env python3
"""Durable native-adapter acceptance, using CARLA's bundled reference driver.

This checks executable scenario integration. Driving success, construction,
scenario activation and reaching the tick budget are reported separately. It is
not an official benchmark evaluation or a training example.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

import numpy as np

from easycarla.config import validate_config
from easycarla.errors import WorldInitializationTimeout
from easycarla.scenarios.bench2drive import Bench2DriveAdapter
from easycarla.simulation.session import WorldSession


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def wait_for_server(host, port, timeout):
    """Read-only readiness before creating a CARLA client with a long RPC timeout."""
    import carla
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        probe = carla.Client(host, port)
        probe.set_timeout(min(1.0, max(0.05, deadline - time.monotonic())))
        try:
            probe.get_server_version()
            probe.get_world()
            return
        except RuntimeError as exc:
            last_error = exc
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
    raise TimeoutError(f'CARLA did not become ready; no episode requested: {last_error}')


def run(args):
    content = args.manifest.read_bytes()
    manifest = json.loads(content)
    chosen = {v for item in args.route for v in item.split(',')}
    entries = [e for e in manifest['entries'] if not chosen or str(e['route_id']) in chosen]
    if chosen - {str(e['route_id']) for e in entries}:
        raise ValueError('Requested route is not present in the explicit manifest')
    # Retain a stable route order while minimizing expensive changes of world.
    entries.sort(key=lambda entry: entry['town'])
    config = validate_config({'simulation': {'port': args.port, 'tm_port': args.tm_port,
                              'reload_on_reset': args.reload, 'timeout': args.timeout,
                              'no_rendering_mode': args.no_rendering, 'map_layers': args.map_layers,
                              'spectator_as_ego': args.spectator_as_ego,
                              'tile_stream_distance': args.tile_stream_distance,
                              'actor_active_distance': args.actor_active_distance}})
    adapter = Bench2DriveAdapter({'manifest': str(args.manifest), 'purpose': manifest['split']})
    report = {'version': 2, 'native_scenario_progress_schema': 1,
              'status': 'running', 'started_utc': datetime.now(timezone.utc).isoformat(),
              'manifest': str(args.manifest.resolve()), 'manifest_sha256': hashlib.sha256(content).hexdigest(),
              'mode': 'native_adapter_reference_driver', 'scope': 'native scenario execution; not policy evaluation',
              'config': config['simulation'], 'seed': args.seed, 'tick_budget': args.ticks,
              'results': [{'route_id': str(e['route_id']), 'town': e['town'], 'scenario_types': e['scenario_types'],
                           'execution': 'not_run'} for e in entries]}
    save(args.output, report)
    try:
        wait_for_server(config['simulation']['host'], args.port, args.timeout)
    except TimeoutError as exc:
        report.update(status='stopped_server_unavailable', stop_reason=str(exc))
        save(args.output, report)
        return report
    session = WorldSession(config)
    from agents.navigation.behavior_agent import BehaviorAgent
    from agents.navigation.local_planner import RoadOption
    import carla
    try:
        for result in report['results']:
            started = time.monotonic()
            episode = None
            result.update(execution='running', ticks=0, activated=[], completed=[])
            save(args.output, report)
            print(f"START {result['route_id']} {result['town']} {','.join(result['scenario_types'])}", flush=True)
            try:
                options = {'route_id': result['route_id']}
                session.load(adapter.town(args.seed, options), args.seed)
                print(f"LOADED {result['route_id']} {session.map.name}", flush=True)
                episode = adapter.prepare(session, args.seed, options)
                print(f"PREPARED {result['route_id']}", flush=True)
                session.controls.handoff(episode.ego, 'reference_driver')
                result['initialization_seconds'] = time.monotonic() - started
                result['metadata'] = dict(episode.metadata)
                result['capabilities'] = episode.capabilities()
                driver = BehaviorAgent(episode.ego, behavior='normal', map_inst=session.map,
                                       grp_inst=episode.provider.get_global_route_planner())
                driver.set_global_plan([(session.map.get_waypoint(t.location), RoadOption.LANEFOLLOW)
                                        for t in episode.route])
                frame = session.world.get_snapshot().frame
                time.sleep(.05)
                if session.world.get_snapshot().frame != frame:
                    raise RuntimeError('World advanced during idle time')
                activated, completed = set(), set()
                progress_transitions, last_progress = [], {}
                native_transforms = 0
                info = episode.info()
                for _ in range(args.ticks):
                    episode.before_tick()
                    control = carla.VehicleControl(brake=1) if driver.done() else driver.run_step()
                    requested = np.array([control.steer, control.throttle, control.brake], dtype=np.float32)
                    resolved = episode.resolve_control(requested)
                    native_transforms += int(not np.array_equal(requested, resolved.values))
                    session.controls.apply_control(episode.ego, resolved.values, owner='reference_driver',
                                                   allow_combined=resolved.allow_combined)
                    snapshot = session.tick()
                    episode.after_tick(snapshot)
                    info = episode.info()
                    result['ticks'] += 1
                    for scenario in info['native_scenarios']:
                        if scenario.get('activation_observed') is True: activated.add(scenario['name'])
                        if scenario.get('completion_observed') is True: completed.add(scenario['name'])
                        signature = (scenario.get('activation_observed'), scenario.get('completion_observed'),
                                     scenario.get('actual_behavior_status'), scenario.get('extraction'))
                        if last_progress.get(scenario['name']) != signature:
                            progress_transitions.append({'frame': snapshot.frame, 'tick': result['ticks'],
                                                         **scenario})
                            last_progress[scenario['name']] = signature
                    if info['native_done']: break
                if session.ticks['step'] != result['ticks']:
                    raise RuntimeError('Hidden native physics tick detected')
                result.update(execution='completed' if info['native_done'] else 'tick_budget_reached',
                              activated=sorted(activated), completed=sorted(completed), events=info,
                              scenario_progress_schema=1, scenario_progress_transitions=progress_transitions,
                              native_success=info['native_success'], native_control_transforms=native_transforms,
                              tick_counts=dict(session.ticks), simulation_seconds=session.timings['simulation_seconds'])
            except Exception as exc:
                result.update(execution='failed', error=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
                if 'time-out' in str(exc).lower() or isinstance(exc, (TimeoutError, WorldInitializationTimeout)):
                    result['execution'] = 'unknown_outcome'
                    report['status'] = 'stopped_unknown_outcome'
            finally:
                if episode is not None:
                    try: episode.close()
                    except Exception as exc:
                        result['cleanup_error'] = f'{type(exc).__name__}: {exc}'
                        result['execution'] = 'failed'
                result['wall_seconds'] = time.monotonic() - started
                try:
                    remaining = [a.type_id for a in session.world.get_actors()
                                 if a.type_id.startswith(('vehicle.', 'walker.', 'sensor.', 'controller.'))]
                    result['remaining_owned_resources'] = len(session.registry.actors)
                    result['immediate_snapshot_dynamic_actors'] = remaining
                    result['cleanup_verification'] = 'owned registry empty after acknowledged destruction; synchronous snapshot may remain stale until next tick'
                    if session.registry.actors:
                        result['execution'] = 'failed'
                        result['cleanup_error'] = 'Resources remain after episode close'
                except Exception as exc:
                    result['server_liveness_error'] = str(exc)
                    report['status'] = 'stopped_server_unavailable'
                save(args.output, report)
                print(f"END {result['route_id']} {result['execution']} {result['ticks']} ticks "
                      f"{result['wall_seconds']:.2f}s {result.get('error','')}", flush=True)
            if report['status'].startswith('stopped_'): break
        else:
            report['status'] = 'finished'
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        raise
    finally:
        try: session.close()
        except Exception as exc:
            report['session_close_error'] = str(exc)
            if report['status'] in ('running', 'finished'):
                report['status'] = 'failed_cleanup'
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save(args.output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=2100)
    parser.add_argument('--tm-port', type=int, default=8100)
    parser.add_argument('--route', action='append', default=[])
    parser.add_argument('--seed', type=int, default=123)
    parser.add_argument('--ticks', type=int, default=1500)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--no-rendering', action='store_true')
    parser.add_argument('--spectator-as-ego', action=argparse.BooleanOptionalAction, default=False,
                        help='Use the spectator as a large-map streaming center when there is no hero')
    parser.add_argument('--map-layers', choices=['all', 'none'], default='all')
    parser.add_argument('--tile-stream-distance', type=float)
    parser.add_argument('--actor-active-distance', type=float)
    parser.add_argument('--reload', action=argparse.BooleanOptionalAction, default=True,
                        help='Reload the world for each route (default); --no-reload explicitly tests same-world reuse')
    args = parser.parse_args()
    if args.ticks <= 0 or args.timeout <= 0: parser.error('ticks and timeout must be positive')
    report = run(args)
    if report['status'] != 'finished' or any(r['execution'] == 'failed' for r in report['results']):
        raise SystemExit(1)

if __name__ == '__main__': main()

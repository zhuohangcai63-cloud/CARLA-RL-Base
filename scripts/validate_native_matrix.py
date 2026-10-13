#!/usr/bin/env python3
"""Run the full explicit native route manifest with one Python/TM lifetime per map.

No simulator restart or step replay is attempted after uncertain execution.
Per-map child reports are kept intact even when a native library aborts.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from validate_native_routes import save


def run(args):
    manifest = json.loads(args.manifest.read_text())
    groups = {}
    results = []
    for entry in manifest['entries']:
        excluded = entry['town'] in args.exclude_town
        if not excluded:
            groups.setdefault(entry['town'], []).append(str(entry['route_id']))
        results.append({'route_id': str(entry['route_id']), 'town': entry['town'],
                        'scenario_types': entry['scenario_types'],
                        'execution': 'blocked' if excluded else 'not_run',
                        'reason': args.exclude_reason if excluded else None})
    report = {'version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'manifest': str(args.manifest.resolve()), 'tick_budget': args.ticks,
              'no_rendering_mode': args.no_rendering, 'map_layers': args.map_layers,
              'spectator_as_ego': args.spectator_as_ego,
              'reload_on_reset': args.reload,
              'tile_stream_distance': args.tile_stream_distance, 'actor_active_distance': args.actor_active_distance,
              'explicitly_excluded_towns': args.exclude_town, 'exclusion_reason': args.exclude_reason,
              'isolation': 'separate adapter/reference-driver Python process per exact map',
              'results': results, 'children': []}
    save(args.output, report)
    by_id = {row['route_id']: row for row in results}
    for town, routes in sorted(groups.items()):
        output = args.output.with_name(args.output.stem + '-' + town + '.json')
        log_path = output.with_suffix('.log')
        command = [sys.executable, str(Path(__file__).with_name('validate_native_routes.py')),
                   '--manifest', str(args.manifest), '--output', str(output), '--port', str(args.port),
                   '--tm-port', str(args.tm_port), '--route', ','.join(routes), '--ticks', str(args.ticks),
                   '--timeout', str(args.timeout), '--map-layers', args.map_layers]
        if args.no_rendering: command.append('--no-rendering')
        command.append('--spectator-as-ego' if args.spectator_as_ego else '--no-spectator-as-ego')
        command.append('--reload' if args.reload else '--no-reload')
        if args.tile_stream_distance is not None: command += ['--tile-stream-distance', str(args.tile_stream_distance)]
        if args.actor_active_distance is not None: command += ['--actor-active-distance', str(args.actor_active_distance)]
        print(f'START MAP {town}: {len(routes)} routes', flush=True)
        with log_path.open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        child = json.loads(output.read_text()) if output.exists() else {'status': 'no_report', 'results': []}
        if child.get('session_close_error') and child['status'] == 'finished':
            child['status'] = 'failed_cleanup'
        report['children'].append({'town': town, 'exit_code': result.returncode, 'status': child['status'],
                                   'report': str(output), 'log': str(log_path)})
        uncertain_child = result.returncode < 0 or child['status'] != 'finished'
        for row in child['results']:
            aggregate_row = dict(row)
            if uncertain_child and row['execution'] == 'running':
                # The child may have aborted inside a CARLA native call, so
                # its last durable record is not a still-running operation.
                # Preserve the child artifact and annotate only the aggregate.
                aggregate_row.update(execution='unknown_outcome',
                    observed_child_execution='running', child_exit_code=result.returncode,
                    uncertainty_reason=f"Child ended with status={child['status']}, exit={result.returncode}; "
                                       "no terminal operation result was recorded; no action replay")
            by_id[row['route_id']].update(aggregate_row)
        save(args.output, report)
        print(f"END MAP {town}: exit={result.returncode} status={child['status']}", flush=True)
        if uncertain_child:
            report['status'] = 'stopped_uncertain_child'
            report['stop_reason'] = f'{town}: child {child["status"]}, exit {result.returncode}; no simulator restart or step replay'
            break
    else:
        report['status'] = 'finished'
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    save(args.output, report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=2100)
    p.add_argument('--tm-port', type=int, default=8100)
    p.add_argument('--ticks', type=int, default=1500)
    p.add_argument('--timeout', type=float, default=90)
    p.add_argument('--no-rendering', action='store_true')
    p.add_argument('--spectator-as-ego', action=argparse.BooleanOptionalAction, default=False)
    p.add_argument('--reload', action=argparse.BooleanOptionalAction, default=True,
                   help='Reload the world for each route (default); --no-reload explicitly tests same-world reuse')
    p.add_argument('--map-layers', choices=['all','none'], default='all')
    p.add_argument('--tile-stream-distance', type=float)
    p.add_argument('--actor-active-distance', type=float)
    p.add_argument('--exclude-town', action='append', default=[])
    p.add_argument('--exclude-reason', default=None)
    a = p.parse_args()
    if a.ticks <= 0 or a.timeout <= 0:p.error('ticks and timeout must be positive')
    if a.exclude_town and not a.exclude_reason:p.error('explicit town exclusion requires --exclude-reason')
    report = run(a)
    if report['status'] != 'finished' or any(r['execution'] == 'failed' for r in report['results']):
        raise SystemExit(1)

if __name__ == '__main__':main()

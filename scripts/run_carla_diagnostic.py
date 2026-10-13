#!/usr/bin/env python3
"""Run one owned CARLA process and one diagnostic client, with durable evidence.

This is a test harness, not a service supervisor: no restart, replay or readiness
ticks. The client must wait for readiness itself. No CARLA Python dependency.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def process_stat(pid):
    """Read /proc without splitting the possibly spaced process name."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'state': fields[0], 'group': int(fields[2]),
                'session': int(fields[3]), 'start_ticks': int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


class OwnedProcess:
    def __init__(self, command, log, *, cwd=None, env=None):
        self.process = subprocess.Popen(command, cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        stat = process_stat(self.process.pid)
        self.start_ticks = stat['start_ticks'] if stat else None
        self.log_error = None
        self.log_eof = False
        try:
            self.pump = threading.Thread(target=self._copy_output, args=(log,), daemon=True)
            self.pump.start()
        except BaseException:
            # The caller has not received this object yet, so it cannot clean up
            # a successfully spawned child if thread creation fails under load.
            try:
                if self.group_alive():
                    os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                self.process.wait(timeout=5)
                self.process.stdout.close()
            raise

    def _copy_output(self, log):
        try:
            with self.process.stdout:
                for line in iter(self.process.stdout.readline, b''):
                    # Keep draining after a disk/write failure so a full pipe
                    # cannot hang the simulator and disguise the evidence error.
                    if self.log_error is None:
                        try:
                            log.write(line.decode('utf-8', errors='replace'))
                            log.flush()
                        except Exception as exc:
                            self.log_error = f'{type(exc).__name__}: {exc}'
                self.log_eof = True
        except Exception as exc:
            self.log_error = f'{type(exc).__name__}: {exc}'

    def group_alive(self):
        # A new session owns this group. Refuse to signal a reused leader PID.
        leader = process_stat(self.process.pid)
        if leader and self.start_ticks is not None and leader['start_ticks'] != self.start_ticks:
            return False
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            stat = process_stat(entry.name)
            if stat and stat['state'] != 'Z' and stat['group'] == self.process.pid and stat['session'] == self.process.pid:
                return True
        return False

    def stop(self, grace):
        signals = []
        leader_exit_before_signal = self.process.poll()
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, max(1.0, grace))):
            self.process.poll()
            if not self.group_alive():
                break
            try:
                if not signals:
                    leader_exit_before_signal = self.process.poll()
                os.killpg(self.process.pid, sig)
                signals.append(sig.name)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                self.process.poll()
                if not self.group_alive():
                    break
                time.sleep(.02)
        self.process.wait(timeout=max(1.0, grace))
        self.pump.join(timeout=max(1.0, grace))
        if not signals:
            leader_exit_before_signal = self.process.returncode
        return {'signals': signals, 'exit_code': self.process.returncode,
                'leader_exit_before_signal': leader_exit_before_signal,
                'log_complete': self.log_eof and self.log_error is None and not self.pump.is_alive(),
                'log_error': self.log_error, 'group_alive': self.group_alive()}


def reserve_ports(stack, port):
    """Cooperative launch leases plus exclusive bind checks; never kill a listener."""
    lock_dir = Path(f'/tmp/easycarla-{os.getuid()}')
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    reservations = []
    for value in range(port, port + 3):
        lease = stack.enter_context(open(lock_dir / f'diagnostic-port-{value}.lock', 'a+'))
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sock = stack.enter_context(socket.socket())
        # An exited TCP server may leave accepted connections in TIME_WAIT.
        # Reusing those tuples is safe; SO_REUSEPORT remains disabled so an
        # active listener is still an exclusive conflict.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', value))
        reservations.append(sock)
    # Check the environment world's lease, but let the launched client acquire it.
    with open(lock_dir / f'world-port-{port}.lock', 'a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return reservations


def resource_sample(pid, elapsed, gpu):
    sample = {'elapsed_seconds': elapsed, 'errors': []}
    for path, key, field in ((Path(f'/proc/{pid}/status'), 'server_rss_kib', 'VmRSS:'),
                             (Path('/proc/meminfo'), 'memory_available_kib', 'MemAvailable:')):
        try:
            lines = path.read_text().splitlines()
            sample[key] = int(next(line for line in lines if line.startswith(field)).split()[1])
        except (OSError, ValueError, StopIteration) as exc:
            sample['errors'].append(f'{key}: {exc}')
    sample['pcie_correctable'] = {}
    for path in Path('/sys/bus/pci/devices').glob('*/aer_dev_correctable'):
        try:
            sample['pcie_correctable'][path.parent.name] = path.read_text().strip()
        except OSError:
            pass
    if gpu:
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=index,memory.used,utilization.gpu,temperature.gpu',
                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=1.0, check=True)
            sample['gpus'] = [dict(zip(('index', 'memory_used_mib', 'utilization_percent', 'temperature_c'),
                                     (float(item.strip()) for item in row.split(','))))
                              for row in result.stdout.splitlines() if row.strip()]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            sample['errors'].append(f'gpu: {exc}')
    return sample


def copy_crashes(saved, destination, pid, started):
    copied, errors = [], []
    for folder in (saved / 'Crashes').glob(f'crashinfo-CarlaUE4-pid-{pid}-*'):
        try:
            if folder.is_symlink() or folder.stat().st_mtime < started - 1:
                continue
            target = destination / 'crashes' / folder.name
            shutil.copytree(folder, target, symlinks=True)
            copied.append(str(target.relative_to(destination)))
        except OSError as exc:
            errors.append(str(exc))
    return copied, errors


def run(args):
    if not 1024 <= args.port <= 65533:
        raise ValueError('port must be in [1024, 65533] (three consecutive ports required)')
    for value in (args.timeout, args.stop_timeout, args.sample_interval):
        if not math.isfinite(value) or value <= 0:
            raise ValueError('timeouts and sample interval must be finite and positive')
    client_command = args.client[1:] if args.client[:1] == ['--'] else args.client
    if not client_command:
        raise ValueError('a client command is required after --')
    for arg in args.server_arg:
        normalized = arg.lower().lstrip('-')
        if normalized.startswith(('carla-', 'world-port', 'primary', 'secondary')):
            raise ValueError('server-arg may not override CARLA endpoints or settings files')
        if normalized.startswith('quality-level'):
            raise ValueError('use --quality-level instead of overriding quality in server-arg')
        if not arg.startswith('-'):
            raise ValueError('server-arg must be an engine flag, not a positional URL')
    binary = args.carla_root.resolve() / 'CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f'CARLA executable not found or not executable: {binary}')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'status': 'starting',
              'started_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'one owned simulator and one client; no readiness, restart or replay',
              'server_exit_before_stop': None, 'peaks': {}, 'sample_count': 0}
    started = time.time()
    started_mono = time.monotonic()
    server = client = None
    interrupted = []
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda signum, frame: interrupted.append(signum))
    try:
        with ExitStack() as stack:
            try:
                reservations = reserve_ports(stack, args.port)
                command = [str(binary), 'CarlaUE4', '-RenderOffScreen', f'-quality-level={args.quality_level}',
                           '-nosound', '-ResX=400', '-ResY=300', *args.server_arg,
                           f'-carla-rpc-port={args.port}', f'-carla-streaming-port={args.port + 1}',
                           f'-carla-secondary-port={args.port + 2}']
                client_command = [item.replace('{OUTPUT_DIR}', str(output)).replace('{PORT}', str(args.port))
                                  for item in client_command]
                report.update(server_command=command, client_command=client_command)
                server_log = stack.enter_context(open(output / 'server.txt', 'x', encoding='utf-8'))
                client_log = stack.enter_context(open(output / 'client.txt', 'x', encoding='utf-8'))
                samples = stack.enter_context(open(output / 'samples.jsonl', 'x', encoding='utf-8'))
                for sock in reservations:
                    sock.close()
                server = OwnedProcess(command, server_log, cwd=args.carla_root.resolve())
                report['server_pid'] = server.process.pid
                env = dict(os.environ, EASYCARLA_DIAGNOSTIC_PORT=str(args.port),
                           EASYCARLA_DIAGNOSTIC_OUTPUT_DIR=str(output),
                           EASYCARLA_DIAGNOSTIC_SERVER_PID=str(server.process.pid))
                client = OwnedProcess(client_command, client_log, env=env)
                report.update(client_pid=client.process.pid, status='running')
                next_sample = 0.0
                while True:
                    elapsed = time.monotonic() - started_mono
                    if elapsed >= next_sample:
                        sample = resource_sample(server.process.pid, elapsed, not args.no_gpu_monitor)
                        samples.write(json.dumps(sample, allow_nan=False) + '\n')
                        samples.flush()
                        report['sample_count'] += 1
                        if 'server_rss_kib' in sample:
                            report['peaks']['server_rss_kib'] = max(
                                report['peaks'].get('server_rss_kib', 0), sample['server_rss_kib'])
                        for gpu in sample.get('gpus', []):
                            key = f'gpu_{int(gpu["index"])}_memory_used_mib'
                            report['peaks'][key] = max(report['peaks'].get(key, 0), gpu['memory_used_mib'])
                        atomic_json(output / 'summary.json', report)
                        next_sample = elapsed + args.sample_interval
                    if interrupted:
                        report.update(status='interrupted', interrupt_signal=interrupted[0])
                        break
                    if server.process.poll() is not None:
                        report['status'] = 'server_exited_during_run'
                        break
                    if client.process.poll() is not None:
                        report['status'] = 'client_passed' if client.process.returncode == 0 else 'client_failed'
                        break
                    if elapsed >= args.timeout:
                        report['status'] = 'timeout'
                        break
                    time.sleep(min(.05, max(0.001, args.timeout - elapsed)))
            except Exception as exc:
                report.update(status='harness_error', error=f'{type(exc).__name__}: {exc}')
            finally:
                if client is not None:
                    report['client_exit_before_stop'] = client.process.poll()
                    try:
                        report['client_cleanup'] = client.stop(args.stop_timeout)
                        report['client_exit'] = client.process.returncode
                    except Exception as exc:
                        report['client_cleanup_error'] = str(exc)
                if server is not None:
                    report['server_exit_before_stop'] = server.process.poll()
                    if report['server_exit_before_stop'] is not None and report['status'] == 'client_passed':
                        report['status'] = 'server_exited_before_stop'
                    report['explicit_server_stop'] = report['server_exit_before_stop'] is None
                    try:
                        report['server_cleanup'] = server.stop(args.stop_timeout)
                        report['server_exit'] = server.process.returncode
                        observed_exit = report['server_cleanup']['leader_exit_before_signal']
                        if observed_exit is not None:
                            # The leader can exit between the outer poll and
                            # stop(). Terminating leftover children does not
                            # turn that runtime exit into a shutdown crash.
                            report['server_exit_before_stop'] = observed_exit
                            report['explicit_server_stop'] = False
                            if report['status'] == 'client_passed':
                                report['status'] = 'server_exited_before_stop'
                        if report['explicit_server_stop']:
                            crash_signals = (signal.SIGABRT, signal.SIGSEGV, signal.SIGBUS,
                                             signal.SIGILL, signal.SIGFPE)
                            report['shutdown_crash'] = server.process.returncode in {
                                code for sig in crash_signals for code in (-sig, 128 + sig)}
                            report['shutdown_forced_kill'] = 'SIGKILL' in report['server_cleanup']['signals']
                            report['shutdown_failed'] = (report['shutdown_forced_kill']
                                or server.process.returncode not in (0, -signal.SIGTERM, 128 + signal.SIGTERM))
                            report['shutdown_outcome'] = (
                                'forced_kill' if report['shutdown_forced_kill'] else
                                'crash_signal' if report['shutdown_crash'] else
                                'exited_zero' if server.process.returncode == 0 else
                                'terminated_by_sigterm' if server.process.returncode == -signal.SIGTERM else
                                'exited_with_sigterm_status' if server.process.returncode == 128 + signal.SIGTERM else
                                'nonzero_exit')
                        else:
                            report['shutdown_crash'] = False
                            report['shutdown_forced_kill'] = False
                            report['shutdown_failed'] = False
                            report['shutdown_outcome'] = 'server_exited_before_stop'
                    except Exception as exc:
                        report['server_cleanup_error'] = str(exc)
                    report['crash_files'], report['crash_copy_errors'] = copy_crashes(
                        args.saved_dir.expanduser(), output, server.process.pid, started)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        report['elapsed_seconds'] = time.monotonic() - started_mono
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        clean = not any(report.get(key) for key in ('client_cleanup_error', 'server_cleanup_error', 'shutdown_failed'))
        clean = clean and all(not report.get(key, {}).get('group_alive')
                              and 'SIGKILL' not in report.get(key, {}).get('signals', [])
                              and report.get(key, {}).get('log_complete', False)
                              for key in ('client_cleanup', 'server_cleanup'))
        report['passed'] = report['status'] == 'client_passed' and clean
        atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--carla-root', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--output-dir', type=Path, required=True, help='must not already exist')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--stop-timeout', type=float, default=15)
    parser.add_argument('--sample-interval', type=float, default=1)
    parser.add_argument('--saved-dir', type=Path, default=Path('~/.config/Epic/CarlaUE4/Saved'))
    parser.add_argument('--quality-level', choices=('Low', 'Epic'), default='Epic',
                        help='Epic by default; Low is an explicit diagnostic comparison')
    parser.add_argument('--server-arg', action='append', default=[], help='repeat as --server-arg=-norhithread')
    parser.add_argument('--no-gpu-monitor', action='store_true')
    parser.add_argument('client', nargs=argparse.REMAINDER, help='-- executable arg...; client handles readiness')
    args = parser.parse_args()
    try:
        report = run(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f'{type(exc).__name__}: {exc}\n')
    print(json.dumps({key: report[key] for key in ('status', 'passed', 'elapsed_seconds')}))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()

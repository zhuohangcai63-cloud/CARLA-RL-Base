"""Diagnostic lifecycle tests use fake executables, never a real simulator."""
import json
import os
from pathlib import Path
import random
import signal
import socket
import subprocess
import sys
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_carla_diagnostic.py'


@pytest.fixture
def fake_carla(tmp_path):
    root = tmp_path / 'fake-carla'
    binary = root / 'CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping'
    binary.parent.mkdir(parents=True)
    binary.write_text(f'#!{sys.executable}\n' + '''
import os, pathlib, resource, signal, subprocess, sys, time
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
root = pathlib.Path.cwd()
mode = (root / 'mode').read_text()
def crash():
    folder = root / 'Saved/Crashes' / ('crashinfo-CarlaUE4-pid-%s-test' % os.getpid())
    folder.mkdir(parents=True)
    (folder / 'report.txt').write_text('fake crash, not CARLA evidence')
    os._exit(139)  # model shell's SIGSEGV status without invoking host core handlers
def stop(signum, frame):
    if mode == 'stop_crash': crash()
    if mode == 'ignore_stop': return
    if mode == 'stop_143': raise SystemExit(143)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
if mode == 'direct_sigterm': signal.signal(signal.SIGTERM, signal.SIG_DFL)
if mode == 'stop_leaves_child':
    child = subprocess.Popen([sys.executable, '-c',
        'import pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'pathlib.Path("child-ready").touch();time.sleep(30)'])
    while not (root / 'child-ready').exists(): time.sleep(.01)
(root / 'pid').write_text(str(os.getpid()))
os.write(1, b'fake server UTF8 validation: \\xff\\n')
while True:
    if mode == 'run_crash' and (root / 'crash-now').exists(): crash()
    if mode == 'run_143' and (root / 'crash-now').exists(): raise SystemExit(143)
    time.sleep(.02)
''', encoding='utf-8')
    binary.chmod(0o755)
    (root / 'mode').write_text('normal')
    return root


def free_ports():
    # Keep probe ports outside the Linux ephemeral range: releasing a port
    # allocated by bind(..., 0) lets an unrelated outbound connection take it
    # before the diagnostic subprocess can perform its own exclusive checks.
    ephemeral_low, ephemeral_high = map(int, Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split())
    candidates = [port for port in range(1024, 65534)
                  if port + 2 < ephemeral_low or port > ephemeral_high]
    for _ in range(50):
        sockets = []
        try:
            first = socket.socket()
            sockets.append(first)
            port = random.choice(candidates)
            first.bind(('0.0.0.0', port))
            for other in (port + 1, port + 2):
                sock = socket.socket()
                sockets.append(sock)
                sock.bind(('0.0.0.0', other))
            return port
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
    raise AssertionError('could not reserve three test ports')


def command(root, output, code, *, port=None, timeout=5, extra=()):
    wait = f'''import pathlib,time,os,signal,sys
root=pathlib.Path({str(root)!r})
deadline=time.monotonic()+4
while not (root/'pid').exists():
    if time.monotonic()>deadline: raise RuntimeError('fake server did not start')
    time.sleep(.01)
'''
    return [sys.executable, str(SCRIPT), '--carla-root', str(root), '--port', str(port or free_ports()),
            '--output-dir', str(output), '--timeout', str(timeout), '--stop-timeout', '.1',
            '--sample-interval', '.05', '--no-gpu-monitor', '--saved-dir', str(root / 'Saved'),
            *extra, '--', sys.executable, '-c', wait + code]


def checked_report(args, output):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    report = json.loads((output / 'summary.json').read_text())
    assert not Path(f'/proc/{report["server_pid"]}').exists()
    assert not report['server_cleanup']['group_alive']
    assert (output / 'server.txt').read_text(encoding='utf-8')
    assert report['elapsed_seconds'] > 0
    return result, report


@pytest.mark.parametrize('mode, code, expected, passed', [
    ('normal', 'print("client passed")', 'client_passed', True),
    ('normal', 'raise SystemExit(7)', 'client_failed', False),
    ('run_crash', '(root/"crash-now").touch(); time.sleep(5)', 'server_exited_during_run', False),
    ('stop_crash', 'print("client passed")', 'client_passed', False),
    ('direct_sigterm', 'print("client passed")', 'client_passed', True),
    ('stop_143', 'print("client passed")', 'client_passed', True),
    ('run_143', '(root/"crash-now").touch(); time.sleep(5)', 'server_exited_during_run', False),
    ('stop_leaves_child', 'print("client passed")', 'client_passed', False),
    ('ignore_stop', 'time.sleep(5)', 'timeout', False),
])
def test_owned_lifecycle(fake_carla, tmp_path, mode, code, expected, passed):
    (fake_carla / 'mode').write_text(mode)
    output = tmp_path / 'evidence'
    args = command(fake_carla, output, code, timeout=.6 if mode == 'ignore_stop' else 5)
    result, report = checked_report(args, output)
    assert report['status'] == expected
    assert report['passed'] is passed
    assert result.returncode == (0 if passed else 1)
    assert report['sample_count'] >= 1
    rows = [json.loads(line) for line in (output / 'samples.jsonl').read_text().splitlines()]
    assert len(rows) == report['sample_count']
    if mode.endswith('crash'):
        assert report['crash_files']
        assert report['shutdown_crash'] is (mode == 'stop_crash')
    if mode == 'ignore_stop':
        assert report['server_cleanup']['signals'] == ['SIGTERM', 'SIGKILL']
        assert report['shutdown_forced_kill']
        assert report['shutdown_failed']
        assert not report['shutdown_crash']
    if mode in ('normal', 'direct_sigterm'):
        assert report['shutdown_outcome'] == ('exited_zero' if mode == 'normal' else 'terminated_by_sigterm')
    if mode == 'stop_leaves_child':
        assert report['server_exit'] == 0
        assert report['shutdown_outcome'] == 'forced_kill'
        assert report['shutdown_failed']
    if mode == 'stop_143':
        assert report['server_exit'] == 143
        assert report['shutdown_outcome'] == 'exited_with_sigterm_status'
        assert report['explicit_server_stop']
        assert not report['shutdown_failed']
    if mode == 'run_143':
        assert report['server_exit_before_stop'] == 143
        assert not report['explicit_server_stop']
        assert report['shutdown_outcome'] == 'server_exited_before_stop'


def test_never_overwrite_output_or_use_occupied_port(fake_carla, tmp_path):
    output = tmp_path / 'existing'
    output.mkdir()
    marker = output / 'summary.json'
    marker.write_text('preserve me')
    result = subprocess.run(command(fake_carla, output, 'pass'), capture_output=True, timeout=5)
    assert result.returncode == 2
    assert marker.read_text() == 'preserve me'
    assert not (fake_carla / 'pid').exists()
    with socket.socket() as external:
        external.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        external.bind(('0.0.0.0', 0))
        external.listen()
        port = external.getsockname()[1]
        output = tmp_path / 'occupied'
        result = subprocess.run(command(fake_carla, output, 'pass', port=port), capture_output=True, timeout=5)
        assert result.returncode == 1
        report = json.loads((output / 'summary.json').read_text())
        assert report['status'] == 'harness_error'
        assert 'server_pid' not in report
        assert external.getsockname()[1] == port
    assert not (fake_carla / 'pid').exists()


def test_preflight_reuses_closed_server_port_in_real_tcp_time_wait(fake_carla, tmp_path):
    port = free_ports()
    with socket.socket() as listener, socket.socket() as peer:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen()
        peer.connect(('127.0.0.1', port))
        accepted, _ = listener.accept()
        # The server actively closes first, so its local port enters TIME_WAIT.
        accepted.shutdown(socket.SHUT_WR)
        accepted.close()
        assert peer.recv(1) == b''
    deadline = time.monotonic() + 1
    while True:
        entries = Path('/proc/net/tcp').read_text().splitlines()[1:]
        if any(int(row.split()[1].split(':')[1], 16) == port
               and row.split()[3] == '06' for row in entries):
            break
        assert time.monotonic() < deadline, 'test did not establish TCP TIME_WAIT'
        time.sleep(.01)
    output = tmp_path / 'time-wait'
    result, report = checked_report(command(fake_carla, output, 'pass', port=port), output)
    assert result.returncode == 0
    assert report['passed']


def test_reject_endpoint_override_before_launch(fake_carla, tmp_path):
    output = tmp_path / 'override'
    args = command(fake_carla, output, 'pass', extra=['--server-arg=-carla-rpc-port=2000'])
    result = subprocess.run(args, capture_output=True, timeout=5)
    assert result.returncode == 2
    assert not (fake_carla / 'pid').exists()
    assert not output.exists()


def test_default_quality_is_one_epic_server_argument(fake_carla, tmp_path):
    output = tmp_path / 'epic-quality'
    result, report = checked_report(command(fake_carla, output, 'pass'), output)
    assert result.returncode == 0
    quality = [arg for arg in report['server_command'] if arg.lower().startswith('-quality-level')]
    assert quality == ['-quality-level=Epic']


def test_quality_cannot_be_duplicated_through_server_arg(fake_carla, tmp_path):
    output = tmp_path / 'ambiguous-quality'
    result = subprocess.run(command(fake_carla, output, 'pass',
        extra=['--server-arg=-quality-level=Epic']), capture_output=True, timeout=5)
    assert result.returncode == 2
    assert not output.exists()
    assert not (fake_carla / 'pid').exists()


def test_failed_client_spawn_still_cleans_owned_server(fake_carla, tmp_path):
    output = tmp_path / 'missing-client'
    args = command(fake_carla, output, 'pass')
    args = args[:args.index('--') + 1] + [str(tmp_path / 'nonexistent-client')]
    result = subprocess.run(args, capture_output=True, timeout=5)
    assert result.returncode == 1
    report = json.loads((output / 'summary.json').read_text())
    assert report['status'] == 'harness_error'
    assert not Path(f'/proc/{report["server_pid"]}').exists()


def test_wrapper_interrupt_cleans_server_and_client(fake_carla, tmp_path):
    output = tmp_path / 'interrupted'
    process = subprocess.Popen(command(fake_carla, output, 'time.sleep(30)'),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            summary = output / 'summary.json'
            if summary.exists() and json.loads(summary.read_text()).get('client_pid'):
                break
            time.sleep(.02)
        else:
            raise AssertionError('harness did not start')
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        report = json.loads(summary.read_text())
        assert report['status'] == 'interrupted'
        assert report['interrupt_signal'] == signal.SIGTERM
        for key in ('server_pid', 'client_pid'):
            assert not Path(f'/proc/{report[key]}').exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cooperative_instance_conflict_never_starts_server(fake_carla, tmp_path):
    import fcntl
    port = free_ports()
    lock_dir = Path(f'/tmp/easycarla-{os.getuid()}')
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    output = tmp_path / 'conflict'
    with open(lock_dir / f'diagnostic-port-{port}.lock', 'a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(command(fake_carla, output, 'pass', port=port), capture_output=True, timeout=5)
    assert result.returncode == 1
    assert not (fake_carla / 'pid').exists()
    assert json.loads((output / 'summary.json').read_text())['status'] == 'harness_error'


def test_client_can_take_world_lease_and_descendants_are_cleaned(fake_carla, tmp_path):
    output = tmp_path / 'descendant'
    code = '''
import fcntl,subprocess
lease=open('/tmp/easycarla-%s/world-port-%s.lock' % (os.getuid(),os.environ['EASYCARLA_DIAGNOSTIC_PORT']),'a+')
fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
(root/'client-child').write_text(str(child.pid))
'''
    result, report = checked_report(command(fake_carla, output, code), output)
    assert result.returncode == 0
    assert report['client_cleanup']['signals'] == ['SIGTERM']
    child_pid = int((fake_carla / 'client-child').read_text())
    stat = Path(f'/proc/{child_pid}/stat')
    assert not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'


def test_log_failure_is_reported_while_pipe_is_drained():
    import importlib.util
    spec = importlib.util.spec_from_file_location('carla_diagnostic_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FullDisk:
        def write(self, value):
            raise OSError('simulated disk full')

    process = module.OwnedProcess([sys.executable, '-c', 'print("x"*1000000)'], FullDisk())
    try:
        process.process.wait(timeout=5)
    finally:
        result = process.stop(.1)
    assert not result['log_complete']
    assert 'simulated disk full' in result['log_error']
    assert result['exit_code'] == 0
    assert result['leader_exit_before_signal'] == 0
    assert result['signals'] == []


def test_thread_start_failure_reaps_child_before_constructor_returns(monkeypatch):
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location('carla_diagnostic_thread_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.subprocess.Popen
    spawned = []

    def record_spawn(*args, **kwargs):
        result = original(*args, **kwargs)
        spawned.append(result)
        return result

    def fail_start(self):
        raise RuntimeError('simulated thread resource exhaustion')

    monkeypatch.setattr(module.subprocess, 'Popen', record_spawn)
    monkeypatch.setattr(module.threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError, match='thread resource exhaustion'):
        module.OwnedProcess([sys.executable, '-c', 'import time;time.sleep(30)'], io.StringIO())
    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    assert not Path(f'/proc/{spawned[0].pid}').exists()

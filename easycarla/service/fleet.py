"""Owned local CARLA/service process groups, explicit lifecycle and durable logs."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from easycarla_client.transport import Names, read_live_record, cleanup_instance


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def process_start(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record):
    return bool(
        record.get("pid")
        and record.get("process_start")
        and process_start(record["pid"]) == record["process_start"]
    )


def memory_available_mb():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("cannot determine memory budget")


def check_ports(ports):
    held = []
    try:
        for port in ports:
            sock = socket.socket()
            held.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
    finally:
        for sock in held:
            sock.close()


def load_fleet(path, *, validate_startup=True):
    from .cli import load_config

    path = Path(path).expanduser().resolve()
    import yaml

    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("fleet config must be a mapping")

    def resolve(value):
        item = Path(value).expanduser()
        return str(
            (path.parent / item).resolve() if not item.is_absolute() else item.resolve()
        )

    directory = Path(resolve(data["log_dir"]))
    python = resolve(data["service_python"]) if validate_startup else None
    root = resolve(data["carla_root"]) if validate_startup else None
    if validate_startup and (not Path(python).is_file() or not os.access(python, os.X_OK)):
        raise ValueError("service_python must be executable")
    if validate_startup and not (Path(root) / "CarlaUE4.sh").is_file():
        raise ValueError("carla_root must contain CarlaUE4.sh")
    threads = data.get(
        "threads", {"rpc": 2, "streaming": 2, "secondary": 2, "numeric": 1}
    )
    if validate_startup and (set(threads) != {"rpc", "streaming", "secondary", "numeric"} or any(
        type(v) is not int or v < 1 for v in threads.values()
    )):
        raise ValueError("thread counts must be positive integers")
    nothreading = data.get("nothreading", False)
    if validate_startup and type(nothreading) is not bool:
        raise ValueError("nothreading must be boolean")
    workers = []
    ports = []
    instances = set()
    for worker in data["workers"]:
        from easycarla.config import validate_config

        instance = worker["instance"]
        Names(instance)
        if instance in instances:
            raise ValueError("duplicate worker instance")
        instances.add(instance)
        worker_id = worker.get("id", instance)
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or any(x in worker_id for x in ("/", "\\"))
            or worker_id in (".", "..")
        ):
            raise ValueError("worker id must be a safe path component")
        if not validate_startup:
            workers.append({"id": worker_id, "instance": instance,
                            "directory": str(directory / worker_id)})
            continue
        config = validate_config(load_config(resolve(worker["config"])))
        allocation = worker["ports"]
        if set(allocation) != {"rpc", "streaming", "secondary", "tm"} or any(
            type(v) is not int or not 1 <= v <= 65535 for v in allocation.values()
        ):
            raise ValueError("specify all four distinct worker ports")
        ports.extend(allocation.values())
        if config["backend"] != "carla" or config["simulation"]["host"] not in (
            "localhost",
            "127.0.0.1",
        ):
            raise ValueError("fleet manages local CARLA backends only")
        if not config["simulation"]["no_rendering_mode"] or any(
            config["observations"][v] for v in ("rgb", "bev", "lidar")
        ):
            raise ValueError(
                "headless fleet requires no_rendering_mode=true and RGB/BEV/LiDAR disabled"
            )
        if (
            config["experiments"]["enabled"]
            and not config["simulation"]["reload_on_reset"]
        ):
            raise ValueError("prefix experiments require reload_on_reset=true")
        config["simulation"].update(
            port=allocation["rpc"], tm_port=allocation["tm"], carla_root=root
        )
        workers.append(
            {
                "id": worker_id,
                "instance": instance,
                "ports": allocation,
                "config": config,
                "python": python,
                "carla_root": root,
                "threads": threads,
                "nothreading": nothreading,
                "service_workdir": resolve(
                    data.get("service_workdir", str(Path.cwd()))
                ),
                "startup_timeout": data.get("startup_timeout", 180.0),
                "directory": str(directory / worker_id),
            }
        )
    if (
        not workers
        or len({w["id"] for w in workers}) != len(workers)
        or (validate_startup and len(set(ports)) != len(ports))
    ):
        raise ValueError("fleet worker IDs and ports must be distinct")
    if not validate_startup:
        return data, directory, workers
    for key in ("startup_timeout", "memory_per_worker_mb", "memory_reserve_mb"):
        value = data.get(
            key,
            {
                "startup_timeout": 180,
                "memory_per_worker_mb": 6000,
                "memory_reserve_mb": 4096,
            }[key],
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < value < float("inf")
        ):
            raise ValueError(f"invalid {key}")
    return data, directory, workers


def stop_group(process, timeout=20.0):
    # Group leaders can exit before their shell children. Reap both cases.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.poll()
        running = False
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == process.pid and fields[0] != "Z":
                    running = True
                    break
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        if not running:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_worker(spec_path):
    # Reap the shipping binary if its launcher shell exits first on SIGTERM.
    import ctypes

    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
    spec = json.loads(Path(spec_path).read_text())
    directory = Path(spec["directory"])
    record_path = directory / "status.json"
    record = {
        "worker_id": spec["id"],
        "instance": spec["instance"],
        "pid": os.getpid(),
        "process_start": process_start(os.getpid()),
        "state": "starting",
        "ports": spec["ports"],
        "service_epoch": None,
        "started_at": time.time(),
        "exit_codes": {},
        "logs": str(directory),
    }
    record["server_thread_limits"] = {
        "requested": {k: spec["threads"][k] for k in ("rpc", "streaming", "secondary")},
        "applied": False,
        "reason": "official_0.9.15_uses_hardware_concurrency; CaRL_flags_are_not_parsed",
    }
    record["numeric_threads"] = spec["threads"]["numeric"]
    atomic_json(record_path, record)
    stopping = False
    processes = {}
    handles = []

    def stop(*args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def persist():
        atomic_json(record_path, record)

    try:
        ports = spec["ports"]
        threads = spec["threads"]
        command = [
            str(Path(spec["carla_root"]) / "CarlaUE4.sh"),
            "-nullrhi",
            "-nosound",
            f'-carla-rpc-port={ports["rpc"]}',
            f'-carla-streaming-port={ports["streaming"]}',
            f'-carla-secondary-port={ports["secondary"]}',
        ]
        if spec["nothreading"]:
            command.append("-nothreading")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        for key in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            env[key] = str(threads["numeric"])

        def launch(name, argv, cwd=None):
            log = open(directory / (name + ".log"), "ab", buffering=0)
            handles.append(log)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            processes[name] = process
            record[name] = {
                "pid": process.pid,
                "process_start": process_start(process.pid),
                "argv": argv,
            }
            persist()
            return process

        server = launch("carla", command, spec["carla_root"])
        deadline = time.monotonic() + spec["startup_timeout"]
        while True:
            if stopping:
                raise InterruptedError("worker start interrupted")
            if server.poll() is not None:
                raise RuntimeError("CARLA exited during startup")
            try:
                # A native CARLA call can block beyond its RPC timeout. Probe in
                # a disposable subprocess so the supervisor remains stoppable.
                probe = (
                    'import carla,json; c=carla.Client("127.0.0.1",'
                    + str(ports["rpc"])
                    + "); "
                    "c.set_timeout(2.); v=c.get_server_version(); w=c.get_world(); "
                    "s=w.get_settings(); s.no_rendering_mode=True; s.synchronous_mode=True; "
                    "s.fixed_delta_seconds="
                    + str(spec["config"]["simulation"]["physics_dt"])
                    + "; "
                    'w.apply_settings(s); print(json.dumps({"version":v,"no_rendering":w.get_settings().no_rendering_mode}))'
                )
                result = subprocess.run(
                    [spec["python"], "-c", probe],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                )
                if result.returncode:
                    raise RuntimeError(result.stderr[-1000:])
                result = json.loads(result.stdout.splitlines()[-1])
                version = result["version"]
                if not version.startswith("0.9.15"):
                    raise ValueError(f"official CARLA 0.9.15 required, got {version}")
                if not result["no_rendering"]:
                    raise ValueError("CARLA refused no_rendering_mode")
                record["carla_version"] = version
                break
            except (RuntimeError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise TimeoutError("CARLA startup timeout")
                time.sleep(0.5)
        atomic_json(directory / "environment.json", spec["config"])
        service = launch(
            "service",
            [
                spec["python"],
                "-m",
                "easycarla.service.cli",
                "serve",
                "--instance",
                spec["instance"],
                "--config",
                str(directory / "environment.json"),
            ],
            spec["service_workdir"],
        )
        while True:
            if stopping:
                raise InterruptedError("worker start interrupted")
            if service.poll() is not None or server.poll() is not None:
                raise RuntimeError("worker exited during startup")
            live = read_live_record(Names(spec["instance"]).server_lock)
            if live and live.get("ready") and live.get("pid") == service.pid:
                record.update(
                    state="ready",
                    service_epoch=live["epoch"],
                    ready_at=time.time(),
                    memory_available_mb=memory_available_mb(),
                )
                persist()
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("service readiness timeout")
            time.sleep(0.1)
        while not stopping:
            if any(p.poll() is not None for p in processes.values()):
                raise RuntimeError("worker process exited; no automatic restart")
            time.sleep(0.2)
        record["state"] = "stopping"
        persist()
    except BaseException as exc:
        record.update(state="failed", error=f"{type(exc).__name__}: {exc}")
        persist()
    finally:
        failed = record["state"] == "failed"
        for name in ("service", "carla"):
            if name in processes:
                stop_group(processes[name])
                record["exit_codes"][name] = processes[name].returncode
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break
        try:
            cleanup_instance(spec["instance"])
        except Exception as exc:
            record["cleanup_error"] = str(exc)
        for handle in handles:
            handle.close()
        record.update(state="failed" if failed else "stopped", stopped_at=time.time())
        persist()


def fleet_command(command, config_path, worker_id=None):
    data, directory, workers = load_fleet(config_path, validate_startup=command not in ("status", "stop"))
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / "fleet.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:

        def status(worker):
            p = Path(worker["directory"]) / "status.json"
            result = (
                json.loads(p.read_text())
                if p.exists()
                else {
                    "worker_id": worker["id"],
                    "instance": worker["instance"],
                    "state": "not_started",
                }
            )
            if result.get("worker_id") != worker["id"] or result.get("instance") != worker["instance"]:
                raise RuntimeError("worker status identity differs from fleet configuration")
            if result.get("state") in ("starting", "ready", "stopping") and not alive(
                result
            ):
                result.update(
                    state="failed",
                    error="supervisor exited; inspect logs and owned process groups",
                )
            return result

        selected = [w for w in workers if worker_id is None or w["id"] == worker_id]
        if not selected:
            raise ValueError("unknown worker ID")
        if command == "status":
            return [status(w) for w in selected]
        if command == "restart" and worker_id is None:
            raise ValueError("restart requires --worker")
        if command in ("stop", "restart"):
            for w in selected:
                record = status(w)
                if alive(record):
                    os.kill(record["pid"], signal.SIGTERM)
                    deadline = time.monotonic() + 60
                    while alive(record) and time.monotonic() < deadline:
                        time.sleep(0.1)
                    if alive(record):
                        raise RuntimeError(
                            "supervisor cleanup timed out; inspect status and logs"
                        )
                elif record.get("state") == "failed":
                    # Recover only PIDs with the original Linux start identity.
                    class OwnedProcess:
                        def __init__(self, identity):
                            self.identity = identity
                            self.pid = identity["pid"]

                        def poll(self):
                            return None if alive(self.identity) else 0

                        def wait(self, timeout):
                            deadline = time.monotonic() + timeout
                            while alive(self.identity) and time.monotonic() < deadline:
                                time.sleep(0.1)
                            if alive(self.identity):
                                raise subprocess.TimeoutExpired(
                                    "owned process", timeout
                                )
                            return None

                    for key in ("service", "carla"):
                        owned = record.get(key, {})
                        if alive(owned):
                            stop_group(OwnedProcess(owned))
                    cleanup_instance(w["instance"])
            if command == "stop":
                return [status(w) for w in selected]
        # Preflight the WHOLE selected allocation before starting any process.
        for w in selected:
            if alive(status(w)) or read_live_record(Names(w["instance"]).server_lock):
                raise RuntimeError(f'worker {w["id"]} already has an owner')
        allocated = [v for w in selected for v in w["ports"].values()]
        release_deadline = time.monotonic() + (65 if command == "restart" else 0)
        while True:
            try:
                check_ports(allocated)
                break
            except OSError:
                if time.monotonic() >= release_deadline:
                    raise
                time.sleep(0.2)
        if memory_available_mb() < data.get("memory_reserve_mb", 4096) + len(
            selected
        ) * data.get("memory_per_worker_mb", 6000):
            raise RuntimeError(
                "insufficient measured available RAM for configured fleet budget"
            )
        started = []
        try:
            for w in selected:
                if memory_available_mb() < data.get(
                    "memory_reserve_mb", 4096
                ) + data.get("memory_per_worker_mb", 6000):
                    raise RuntimeError("available RAM fell below startup budget")
                wd = Path(w["directory"])
                wd.mkdir(parents=True, exist_ok=True)
                if (wd / "status.json").exists():
                    (wd / "status.json").rename(wd / f"status-{time.time_ns()}.json")
                spec_path = wd / "worker.json"
                atomic_json(spec_path, w)
                with open(wd / "supervisor.log", "ab", buffering=0) as log:
                    p = subprocess.Popen(
                        [
                            w["python"],
                            "-m",
                            "easycarla.service.fleet",
                            "--spec",
                            str(spec_path),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                started.append((w, p))
                deadline = time.monotonic() + w["startup_timeout"] + 10
                while True:
                    record = status(w)
                    if record.get("state") == "ready" and record.get("pid") == p.pid:
                        break
                    if p.poll() is not None or record.get("state") == "failed":
                        raise RuntimeError(f'worker {w["id"]} failed: {record}')
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'worker {w["id"]} startup timed out')
                    time.sleep(0.2)
            return [status(w) for w in selected]
        except BaseException:
            for w, p in started:
                if p.poll() is None:
                    p.terminate()
            for w, p in started:
                try:
                    p.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    pass
            raise
    finally:
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    run_worker(parser.parse_args().spec)

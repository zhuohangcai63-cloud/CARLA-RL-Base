"""Completion-order requests across independent synchronous IPC clients."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import inspect
import threading
from queue import SimpleQueue, Empty
import numpy as np
from .client import CarlaEnvClient
from .errors import is_request_rejection


_SIGNATURES = {name: inspect.signature(getattr(CarlaEnvClient, name)) for name in
               ("step", "step_trajectory", "reset", "capabilities", "get_static", "get_route")}


def validate_control(action):
    values = np.asarray(action, dtype=np.float64)
    if values.shape != (3,) or not np.isfinite(values).all():
        raise ValueError("control requires finite [steer,throttle,brake]")
    if (
        np.any(values < [-1, 0, 0])
        or np.any(values > [1, 1, 1])
        or (values[1] > 0 and values[2] > 0)
    ):
        raise ValueError("control bounds or simultaneous throttle/brake are invalid")
    return values.astype(np.float32)


def validate_trajectory(trajectory, execute_seconds=None):
    """Validate numeric request data without simulator state or clock queries."""
    if not isinstance(trajectory, dict):
        raise ValueError("trajectory must be a mapping")
    frame = trajectory.get("reference_frame")
    if isinstance(frame, (bool, np.bool_)) or not isinstance(frame, (int, np.integer)) or frame < 0:
        raise ValueError("trajectory reference_frame must be a nonnegative integer")
    if trajectory.get("reference_point", "actor") not in ("actor", "rear_axle"):
        raise ValueError("reference_point must be actor or rear_axle")
    times = np.asarray(trajectory.get("times"), dtype=np.float64)
    points = np.asarray(trajectory.get("points"), dtype=np.float64)
    if times.ndim != 1 or len(times) < 2 or points.shape != (len(times), 4):
        raise ValueError("trajectory requires times[N] and points[N,4]")
    if not np.isfinite(times).all() or not np.isfinite(points).all():
        raise ValueError("trajectory must be finite")
    if times[0] != 0 or np.any(np.diff(times) <= 0) or np.any(points[:, 3] < 0):
        raise ValueError("times must start at zero and increase; speeds must be nonnegative")
    if execute_seconds is not None:
        duration = float(execute_seconds)
        if not np.isfinite(duration) or duration <= 0 or duration > times[-1] + 1e-9:
            raise ValueError("execution duration must be positive and covered by the trajectory")


@dataclass(frozen=True)
class Completion:
    worker_id: object
    result: object = None
    error: BaseException | None = None


class ClientPool:
    """One outstanding request per worker; no retries, resets or policy decisions.

    Clients are supplied and owned by the caller. Consumption releases a worker
    slot. Failed workers remain disabled until explicitly replaced.
    """

    def __init__(self, clients):
        self.clients = dict(clients)
        if not self.clients or len({id(c) for c in self.clients.values()}) != len(
            self.clients
        ):
            raise ValueError("pool requires distinct clients")
        self._executor = ThreadPoolExecutor(
            max_workers=len(self.clients), thread_name_prefix="easycarla"
        )
        self._pending = {}
        self._failed = set()
        self._lock = threading.Lock()
        self._closed = False
        self._ready = SimpleQueue()

    def submit(self, worker_id, operation, *args, **kwargs):
        self.submit_batch({worker_id: (operation, args, kwargs)})

    def submit_batch(self, requests):
        prepared = {}
        for worker_id, (operation, args, kwargs) in requests.items():
            if operation not in _SIGNATURES:
                raise ValueError("unsupported pool operation")
            args, kwargs = deepcopy((args, kwargs))
            bound = _SIGNATURES[operation].bind(None, *args, **kwargs)
            if operation == "step":
                if len(args) != 1 or kwargs:
                    raise ValueError("step requires one control")
                args = (validate_control(args[0]),)
            elif operation == "step_trajectory":
                validate_trajectory(bound.arguments["trajectory"], bound.arguments.get("execute_seconds"))
            elif operation == "reset":
                seed, options = bound.arguments.get("seed"), bound.arguments.get("options")
                if seed is not None and (isinstance(seed, (bool, np.bool_)) or
                                         not isinstance(seed, (int, np.integer))):
                    raise ValueError("reset seed must be an integer or None")
                if options is not None and not isinstance(options, dict):
                    raise ValueError("reset options must be a mapping")
            prepared[worker_id] = (operation, args, kwargs)
        with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed")
            for worker_id in prepared:
                if worker_id not in self.clients:
                    raise KeyError(worker_id)
                if worker_id in self._pending:
                    raise RuntimeError("worker already has a request in flight")
                if worker_id in self._failed:
                    raise RuntimeError("failed worker requires explicit replacement")
                inspect.signature(getattr(self.clients[worker_id], prepared[worker_id][0])).bind(
                    *prepared[worker_id][1], **prepared[worker_id][2])
            for worker_id, (operation, args, kwargs) in prepared.items():
                client = self.clients[worker_id]
                self._pending[worker_id] = self._executor.submit(
                    getattr(client, operation), *args, **kwargs
                )
                self._pending[worker_id].add_done_callback(
                    lambda future, worker=worker_id: self._ready.put((worker, future))
                )

    def poll_ready(self, timeout=0.0):
        if not np.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        with self._lock:
            pending = dict(self._pending)
        if not pending:
            return []
        ready = []
        try:
            ready.append(self._ready.get(timeout=timeout))
        except Empty:
            return []
        while True:
            try:
                ready.append(self._ready.get_nowait())
            except Empty:
                break
        results = []
        with self._lock:
            for worker, future in ready:
                if self._pending.get(worker) is not future or not future.done():
                    continue
                del self._pending[worker]
                try:
                    results.append(Completion(worker, result=future.result()))
                except BaseException as exc:
                    if not is_request_rejection(exc):
                        self._failed.add(worker)
                    results.append(Completion(worker, error=exc))
        return results

    def replace(self, worker_id, client):
        with self._lock:
            if worker_id in self._pending:
                raise RuntimeError("cannot replace a pending worker")
            if worker_id not in self.clients:
                raise KeyError(worker_id)
            if client is self.clients[worker_id]:
                raise ValueError("replacement must use a different client")
            if any(client is c for k, c in self.clients.items() if k != worker_id):
                raise ValueError("client already belongs to another worker")
            self.clients[worker_id] = client
            self._failed.discard(worker_id)

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

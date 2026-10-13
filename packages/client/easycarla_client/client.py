"""Synchronous single-controller client; importing this package never imports CARLA."""
import os
import math
import threading
import time
import uuid

import posix_ipc

from .errors import ClientBusy, ProtocolError, RemoteError, ServiceUnavailable, UnknownOutcomeError
from .protocol import copy_observation, encode, read_slot, write_slot
from .transport import FileLock, Names, SharedResources, read_live_record


class ExperimentClient:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, method):
        if method.startswith("_"):
            raise AttributeError(method)
        def call(**kwargs):
            return self._client._request("EXPERIMENT", {"method": method, "kwargs": kwargs})
        return call


class CarlaEnvClient:
    def __init__(self, instance_name="carla_01", *, timeout=30.0, copy=True):
        if isinstance(timeout, bool):
            raise ValueError("timeout must be positive and finite")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be positive and finite") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.instance_name = instance_name
        self.timeout = float(timeout)
        self.copy = bool(copy)
        self._pid = os.getpid()
        self._mutex = threading.Lock()
        self._resources = None
        self._owner = None
        self._closed = False
        self._uncertain = False
        self._sequence = 0
        self._static_cache = {}
        self._route_cache = {}
        self.last_request_seconds = 0.0
        self.last_timings = {}
        self._names = Names(instance_name)
        self._session = str(uuid.uuid4())
        self.experiment = ExperimentClient(self)
        try:
            self._owner = FileLock(self._names.client_lock)
            if not self._owner.acquire(timeout=min(.25, self.timeout)):
                raise ClientBusy(f"Instance {instance_name!r} already has a control client")
            record = read_live_record(self._names.server_lock)
            if not record or not record.get("ready"):
                raise ServiceUnavailable(f"Instance {instance_name!r} is not ready")
            self._epoch = record["epoch"]
            self._resources = SharedResources(self._names)
            self._owner.write({"pid": os.getpid(), "session": self._session, "epoch": self._epoch})
            # Never write into a previous client's pending request. The service first
            # finishes/discards that request, cleans its episode, then publishes CLAIMED.
            deadline = time.monotonic() + self.timeout
            while True:
                record = self._check_service()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ServiceUnavailable("Timed out awaiting exclusive session claim; no request submitted")
                if record.get("session") != self._session:
                    time.sleep(min(0.005, remaining))
                    continue
                try:
                    self._resources.result_ready.acquire(timeout=min(0.1, remaining))
                except posix_ipc.BusyError:
                    continue
                header, result = read_slot(self._resources.maps["response"])
                if header.epoch == self._epoch and header.session == self._session and header.operation == "CLAIMED":
                    self.server_info = result
                    break
        except Exception:
            self._disconnect()
            raise

    def _check_service(self):
        if os.getpid() != self._pid:
            raise ClientBusy("A client cannot be reused after fork; create it in the child process")
        record = read_live_record(self._names.server_lock)
        if not record or record.get("epoch") != self._epoch or not record.get("ready"):
            raise ServiceUnavailable("Service stopped or restarted; reconnect and reset explicitly")
        return record

    def _request(self, operation, body, *, copy=None):
        if not self._mutex.acquire(blocking=False):
            raise ClientBusy("Only one request may be in flight per client")
        try:
            return self._exchange(operation, body, copy=copy)
        finally:
            self._mutex.release()

    def _exchange(self, operation, body, *, copy=None):
        """Caller holds _mutex through exchange and any connection teardown."""
        submitted = False
        started = time.monotonic()
        self.last_timings = {"backend_seconds": 0.0}
        try:
            if self._closed:
                raise ServiceUnavailable("Client is closed")
            if self._uncertain:
                raise UnknownOutcomeError("Previous result is unknown; close this client and create a new session")
            self._check_service()
            encode_started = time.monotonic()
            payload = encode(body)
            self.last_timings["request_encode_seconds"] = time.monotonic() - encode_started
            next_sequence = self._sequence + 1
            write_slot(self._resources.maps["request"], epoch=self._epoch, session=self._session,
                       sequence=next_sequence, operation=operation, payload=payload)
            self._sequence = next_sequence
            # Once notification is attempted, an interruption may occur after
            # sem_post succeeded. Conservatively report unknown outcome even
            # if release itself raises; never classify this as safe to replay.
            submitted = True
            self._resources.request_ready.release()
            deadline = time.monotonic() + self.timeout
            while True:
                self._check_service()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Request {self._sequence} timed out")
                try:
                    self._resources.result_ready.acquire(timeout=min(0.1, remaining))
                except posix_ipc.BusyError:
                    continue
                decode_started = time.monotonic()
                header, result = read_slot(self._resources.maps["response"], copy=self.copy if copy is None else copy)
                self.last_timings["response_decode_seconds"] = time.monotonic() - decode_started
                if header.epoch != self._epoch or header.session != self._session:
                    raise ProtocolError("Response belongs to another service epoch or session")
                if header.sequence != self._sequence or header.operation != operation:
                    raise ProtocolError("Response does not match the outstanding request")
                self.last_timings["backend_seconds"] = result.get("backend_seconds", 0.0)
                if not result["ok"]:
                    error = result["error"]
                    raise RemoteError(error["code"], error["message"], details=error.get("details"))
                if operation in ("STATIC", "ROUTE"):
                    static_header, data = read_slot(self._resources.maps["static"], copy=True)
                    if (static_header.epoch, static_header.session, static_header.sequence, static_header.operation) != (self._epoch, self._session, self._sequence, operation):
                        raise ProtocolError("Static data publication does not match request")
                    return data
                return result.get("value")
        except RemoteError:
            raise
        except BaseException as exc:
            if submitted:
                self._uncertain = True
                raise UnknownOutcomeError(f"{operation} outcome is unknown: {exc}; request will not be replayed") from exc
            raise
        finally:
            self.last_request_seconds = time.monotonic() - started
            self.last_timings["roundtrip_seconds"] = self.last_request_seconds
            self.last_timings["transport_and_copy_seconds"] = max(0.0, self.last_request_seconds - self.last_timings.get("backend_seconds", 0.0))

    def reset(self, *, seed=None, options=None):
        return self._request("RESET", {"seed": seed, "options": options})

    def step(self, action):
        return self._request("STEP", {"action": action})

    def step_trajectory(self, trajectory, execute_seconds=None):
        return self._request("TRAJECTORY", {"trajectory": trajectory, "execute_seconds": execute_seconds})

    def capabilities(self):
        return self._request("CAPABILITIES", {})

    def get_static(self, map_id=None):
        if map_id is not None and map_id in self._static_cache:
            return copy_observation(self._static_cache[map_id])
        data = self._request("STATIC", {"map_id": map_id}, copy=True)
        key = map_id if map_id is not None else data.get("map_id") if isinstance(data, dict) else None
        if key is not None:
            if key not in self._static_cache and len(self._static_cache) >= 4:
                self._static_cache.pop(next(iter(self._static_cache)))
            self._static_cache[key] = copy_observation(data)
        return data

    def get_route(self, route_id=None):
        if route_id is not None and route_id in self._route_cache:
            return copy_observation(self._route_cache[route_id])
        data = self._request("ROUTE", {"route_id": route_id}, copy=True)
        key = data["route_id"]
        # A route query is cached locally; bounded to avoid retaining a training
        # run's entire collection of episode routes.
        if len(self._route_cache) >= 8:
            self._route_cache.pop(next(iter(self._route_cache)))
        self._route_cache[key] = copy_observation(data)
        return data

    @staticmethod
    def copy_observation(observation):
        return copy_observation(observation)

    def _disconnect(self):
        self._closed = True
        if self._resources is not None:
            self._resources.close()
            self._resources = None
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    def close(self):
        if self._closed:
            return
        if not self._mutex.acquire(blocking=False):
            raise ClientBusy("Cannot close while another request is in flight")
        try:
            if not self._uncertain:
                self._exchange("CLOSE", {}, copy=True)
        finally:
            self._disconnect()
            self._mutex.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def __del__(self):
        # Destructors never send commands or replay an in-flight operation.
        try:
            self._disconnect()
        except Exception:
            pass

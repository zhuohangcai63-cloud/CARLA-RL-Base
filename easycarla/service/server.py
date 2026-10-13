"""Single-threaded environment command server with at-most-once session dispatch."""
import logging
import os
import time
import uuid
from easycarla.errors import RequestRejected

import posix_ipc

from easycarla_client.errors import InstanceBusy, ProtocolError
from easycarla_client.protocol import (DEFAULT_REQUEST_BYTES, DEFAULT_RESPONSE_BYTES,
    DEFAULT_STATIC_BYTES, encode, read_slot, write_slot)
from easycarla_client.transport import (FileLock, Names, SharedResources, drain,
    read_live_record, remove_stale_resources)

LOG = logging.getLogger(__name__)


class EnvironmentService:
    """One backend per service; episode and client-session cleanup are idempotent."""
    def __init__(self, instance_name, backend_factory, *, request_bytes=DEFAULT_REQUEST_BYTES,
                 response_bytes=DEFAULT_RESPONSE_BYTES, static_bytes=DEFAULT_STATIC_BYTES):
        self.names = Names(instance_name)
        self.backend_factory = backend_factory
        self.capacities = {"request": request_bytes, "response": response_bytes, "static": static_bytes}
        self.epoch = str(uuid.uuid4())
        self.resources = None
        self.owner = None
        self.backend = None
        self.session = None
        self.sequence = 0
        self._cached = None
        self._stop = False
        self._episode_closed = True
        self._client_closed = True
        self.last_timings = {}

    def start(self):
        if self.owner is not None:
            raise RuntimeError("Service is already started")
        self.owner = FileLock(self.names.server_lock)
        if not self.owner.acquire(timeout=.25):
            self.owner.close()
            self.owner = None
            raise InstanceBusy(f"Instance {self.names.instance!r} already has a service")
        try:
            self.owner.write({"pid": os.getpid(), "epoch": self.epoch, "ready": False})
            remove_stale_resources(self.names)
            self.resources = SharedResources(self.names, create=True, capacities=self.capacities)
            self.backend = self.backend_factory()
            self.owner.write({"pid": os.getpid(), "epoch": self.epoch, "ready": True})
        except Exception:
            self.shutdown()
            raise
        return self

    def _close_episode(self):
        if not self._episode_closed:
            self.backend.close_episode()
            self._episode_closed = True

    def _close_client_session(self):
        if not self._client_closed:
            self.backend.close_client_session()
            self._episode_closed = True
            self._client_closed = True

    def _reconcile_owner(self):
        record = read_live_record(self.names.client_lock)
        session = record.get("session") if record and record.get("epoch") == self.epoch else None
        try:
            if session is not None:
                uuid.UUID(session)
        except (ValueError, TypeError):
            session = None
        if session == self.session:
            return
        # This is called only outside command execution. A new client is not permitted
        # to write the request slot until its CLAIMED publication below.
        try:
            self._close_client_session()
        except Exception:
            LOG.exception("Episode cleanup failed during client handover")
            # A broken cleanup cannot safely grant a new world owner.
            raise
        drain(self.resources.request_ready)
        drain(self.resources.result_ready)
        self.session = session
        self._client_closed = session is None
        self.sequence = 0
        self._cached = None
        if session is not None:
            write_slot(self.resources.maps["response"], epoch=self.epoch, session=session,
                       sequence=0, operation="CLAIMED",
                       value={"instance_name": self.names.instance, "epoch": self.epoch,
                              "protocol_version": 1, "capacities": self.capacities})
            self.resources.result_ready.release()
        # New clients must not consume an abandoned result notification while this
        # slot is being replaced by CLAIMED. Publish lifecycle ownership last.
        self.owner.write({"pid": os.getpid(), "epoch": self.epoch, "ready": True, "session": session})

    def _error(self, code, message, **details):
        return {"ok": False, "error": {"code": code, "message": str(message), "details": details}}

    def _dispatch(self, operation, data):
        if not isinstance(data, dict):
            raise ValueError("Request body must be a dictionary")
        if operation == "RESET":
            self._episode_closed = False  # Partial initialization must also be reclaimed.
            self._client_closed = False
            return self.backend.reset(seed=data.get("seed"), options=data.get("options"))
        if operation == "STEP":
            return self.backend.step(data["action"])
        if operation == "TRAJECTORY":
            return self.backend.step_trajectory(data["trajectory"], execute_seconds=data.get("execute_seconds"))
        if operation == "STATIC":
            return self.backend.get_static(map_id=data.get("map_id"))
        if operation == "ROUTE":
            return self.backend.get_route(route_id=data.get("route_id"))
        if operation == "CAPABILITIES":
            result = dict(self.backend.capabilities())
            result["service_timings"] = dict(self.last_timings)
            return result
        if operation == "EXPERIMENT":
            method = data["method"]
            if not isinstance(method, str) or method.startswith("_"):
                raise ValueError("Invalid experiment operation")
            return self.backend.experiment(method, **data.get("kwargs", {}))
        if operation == "CLOSE":
            self._close_client_session()
            return None
        raise ValueError(f"Unknown operation {operation!r}")

    def _publish(self, header, envelope=None, *, payload=None):
        started = time.monotonic()
        write_slot(self.resources.maps["response"], epoch=self.epoch, session=header.session,
                   sequence=header.sequence, operation=header.operation, value=envelope, payload=payload)
        self.resources.result_ready.release()
        self.last_timings["response_publish_seconds"] = time.monotonic() - started

    def process_one(self):
        header, data = read_slot(self.resources.maps["request"])
        if header.epoch != self.epoch or header.session != self.session:
            self._publish(header, self._error("stale_session", "Request epoch or session is no longer active"))
            return
        fingerprint = (header.operation, header.digest)
        if header.sequence == self.sequence and self._cached is not None:
            if fingerprint == self._cached[0]:
                self._publish(header, payload=self._cached[1])
            else:
                self._publish(header, self._error("sequence_conflict", "Sequence was already used with different content"))
            return
        if header.sequence != self.sequence + 1:
            self._publish(header, self._error("invalid_sequence", "Only the next request or last exact duplicate is accepted",
                                             last_sequence=self.sequence))
            return
        started = time.monotonic()
        lifecycle_before = self._episode_closed, self._client_closed
        try:
            value = self._dispatch(header.operation, data)
            backend_seconds = time.monotonic() - started
            if header.operation in ("STATIC", "ROUTE"):
                write_slot(self.resources.maps["static"], epoch=self.epoch, session=self.session,
                           sequence=header.sequence, operation=header.operation, value=value)
                value = None
            envelope = {"ok": True, "value": value}
        except RequestRejected as exc:
            backend_seconds = time.monotonic() - started
            self._episode_closed, self._client_closed = lifecycle_before
            envelope = self._error("invalid_request", exc, exception=type(exc).__name__,
                                   request_rejected=True)
        except (ValueError, TypeError, KeyError) as exc:
            backend_seconds = time.monotonic() - started
            envelope = self._error("invalid_request", exc, exception=type(exc).__name__)
            if header.operation == "RESET":
                self._close_episode()
        except Exception as exc:
            backend_seconds = time.monotonic() - started
            LOG.exception("Backend command %s failed", header.operation)
            envelope = self._error(getattr(exc, "code", "backend_error"), exc,
                                   exception=type(exc).__name__, episode_faulted=True)
            # Runtime diagnostics (for example affected sensor frames) survive IPC.
            # Backend-provided context cannot overwrite server-owned status fields.
            context = getattr(exc, "details", {})
            if isinstance(context, dict):
                for key, value in context.items():
                    envelope["error"]["details"].setdefault(key, value)
            self._close_episode()
        envelope["backend_seconds"] = backend_seconds
        try:
            encode_started = time.monotonic()
            payload = encode(envelope)
            self.last_timings = {"operation": header.operation, "sequence": header.sequence,
                                 "response_encode_seconds": time.monotonic() - encode_started}
            if len(payload) > len(self.resources.maps["response"]) - 256:
                raise ProtocolError("Result exceeds response capacity")
        except ProtocolError as exc:
            self._close_episode()
            payload = encode(self._error("result_encoding_failed", exc, episode_faulted=True,
                                         command_may_have_executed=True))
        # Cache before publication. No other operation can overwrite the cache while
        # the synchronous single controlling client is consuming this response.
        self.sequence = header.sequence
        self._cached = (fingerprint, payload)
        self._publish(header, payload=payload)

    def serve_forever(self, *, stop_event=None, ready_event=None):
        try:
            if self.owner is None:
                self.start()
            if ready_event is not None:
                ready_event.set()
            while not self._stop and not (stop_event is not None and stop_event.is_set()):
                self._reconcile_owner()
                try:
                    self.resources.request_ready.acquire(timeout=0.05)
                except posix_ipc.BusyError:
                    continue
                except posix_ipc.SignalError:
                    # SIGTERM/SIGINT can interrupt sem_timedwait after the handler
                    # requests shutdown. This is a clean stop, not a backend fault.
                    if self._stop or (stop_event is not None and stop_event.is_set()):
                        break
                    continue
                # Check after wake: a dead client's abandoned publication is not an
                # authorization to execute its action for the next session.
                previous = self.session
                self._reconcile_owner()
                if previous is not None and previous == self.session:
                    self.process_one()
        finally:
            self.shutdown()

    def stop(self):
        self._stop = True

    def shutdown(self):
        try:
            if self.backend is not None:
                self.backend.shutdown()
        finally:
            self.backend = None
            if self.resources is not None:
                self.resources.close(unlink=True)
                self.resources = None
            if self.owner is not None:
                self.owner.close()
                self.owner = None

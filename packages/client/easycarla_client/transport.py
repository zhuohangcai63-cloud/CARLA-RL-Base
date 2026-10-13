"""POSIX resource and lifecycle helpers shared by client and service."""
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import time

import posix_ipc

from .errors import InstanceBusy, ProtocolError
from .protocol import HEADER_BYTES


class Names:
    def __init__(self, instance):
        if not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", instance):
            raise ValueError("instance_name must contain 1..64 ASCII letters, digits, '_', '.', or '-'")
        self.instance = instance
        token = hashlib.sha256(instance.encode()).hexdigest()[:24]
        self.prefix = f"/easycarla_{os.getuid()}_{token}"
        self.directory = Path(f"/tmp/easycarla-{os.getuid()}")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        stat = self.directory.lstat()
        if not self.directory.is_dir() or self.directory.is_symlink() or stat.st_uid != os.getuid():
            raise ProtocolError("Unsafe lifecycle lock directory")
        if stat.st_mode & 0o077:
            raise ProtocolError("Lifecycle lock directory must have permissions 0700")
        self.server_lock = self.directory / f"{token}.server.lock"
        self.client_lock = self.directory / f"{token}.client.lock"

    def shm(self, slot):
        return self.prefix + "_" + slot

    def sem(self, direction):
        return self.prefix + "_" + direction


class FileLock:
    """Lock files are persistent: never unlink or replace their inode."""
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        self.held = False
        self.pid = os.getpid()

    def acquire(self, timeout=0.0):
        # Liveness probes briefly acquire an otherwise free flock. Actual owners
        # allow that probe to finish instead of misreporting a live controller.
        # Probes call the zero-timeout form and must never wait on a real owner.
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return True
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(.001, remaining))

    def release(self):
        if self.held and os.getpid() == self.pid:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.held = False

    def write(self, record):
        if not self.held:
            raise RuntimeError("Lifecycle metadata requires ownership")
        data = json.dumps(record, separators=(",", ":")).encode()
        if len(data) > 4096:
            raise ValueError("Lifecycle record too large")
        # Fixed-size records avoid a read between a shorter write and truncate.
        os.pwrite(self.fd, data.ljust(4096, b" "), 0)

    def read(self):
        try:
            return json.loads(os.pread(self.fd, 4096, 0))
        except (ValueError, OSError):
            return {}

    def close(self):
        if self.fd is not None:
            self.release()
            os.close(self.fd)
            self.fd = None


def read_live_record(path):
    lock = FileLock(path)
    try:
        if lock.acquire():
            return None
        return lock.read()
    finally:
        lock.close()


def drain(semaphore):
    while True:
        try:
            semaphore.acquire(timeout=0)
        except posix_ipc.BusyError:
            return


class SharedResources:
    def __init__(self, names, *, create=False, capacities=None):
        self.names = names
        self.maps = {}
        self.semaphores = {}
        self.owned = []
        try:
            for slot in ("request", "response", "static"):
                size = capacities[slot] if create else 0
                if create and size < HEADER_BYTES + 1024:
                    raise ValueError("Each shared-memory slot must be at least 1280 bytes")
                mem = posix_ipc.SharedMemory(names.shm(slot),
                    flags=posix_ipc.O_CREX if create else 0, mode=0o600, size=size)
                if create:
                    self.owned.append(("shm", names.shm(slot)))
                try:
                    access = mmap.ACCESS_WRITE if create or slot == "request" else mmap.ACCESS_READ
                    self.maps[slot] = mmap.mmap(mem.fd, mem.size, access=access)
                finally:
                    mem.close_fd()
            for direction in ("request_ready", "result_ready"):
                sem = posix_ipc.Semaphore(names.sem(direction),
                    flags=posix_ipc.O_CREX if create else 0, mode=0o600, initial_value=0)
                self.semaphores[direction] = sem
                if create:
                    self.owned.append(("sem", names.sem(direction)))
        except Exception:
            self.close(unlink=create)
            raise

    @property
    def request_ready(self):
        return self.semaphores["request_ready"]

    @property
    def result_ready(self):
        return self.semaphores["result_ready"]

    def close(self, *, unlink=False):
        for mapping in self.maps.values():
            try:
                mapping.close()
            except BufferError:
                # A borrowed ndarray keeps the mmap alive until its own release.
                pass
        self.maps.clear()
        for sem in self.semaphores.values():
            sem.close()
        self.semaphores.clear()
        if unlink:
            for kind, name in self.owned:
                try:
                    (posix_ipc.unlink_shared_memory if kind == "shm" else posix_ipc.unlink_semaphore)(name)
                except posix_ipc.ExistentialError:
                    pass
        self.owned.clear()


def remove_stale_resources(names):
    """Caller must exclusively hold names.server_lock."""
    for slot in ("request", "response", "static"):
        try:
            posix_ipc.unlink_shared_memory(names.shm(slot))
        except posix_ipc.ExistentialError:
            pass
    for direction in ("request_ready", "result_ready"):
        try:
            posix_ipc.unlink_semaphore(names.sem(direction))
        except posix_ipc.ExistentialError:
            pass


def cleanup_instance(instance_name):
    """Remove crash residue only when no service owns the instance."""
    names = Names(instance_name)
    lock = FileLock(names.server_lock)
    try:
        if not lock.acquire(timeout=.25):
            raise InstanceBusy(f"Service {instance_name!r} is alive; refusing cleanup")
        remove_stale_resources(names)
    finally:
        lock.close()

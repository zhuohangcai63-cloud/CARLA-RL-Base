"""Aggregate-bounded prefix storage with ordered, checksummed IPC chunks."""
from dataclasses import dataclass
import hashlib
import json
import uuid

import numpy as np

from .replay import PrefixCapacityError, validate_prefix

PREFIX_CHUNK_BYTES = 64 * 1024
INLINE_PREFIX_BYTES = 512 * 1024


def encode_material(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


@dataclass
class Material:
    data: bytes | bytearray
    sha256: str
    complete: bool
    offset: int = 0

    @property
    def size_bytes(self):
        return len(self.data)


class PrefixMaterialStore:
    """Owned by one ExperimentSession; all handles are local to that service.

    Limits cover complete materials and in-progress imports together. Failed
    imports remain reserved until explicitly released. Reset never invalidates
    handles; session/service close clears them.
    """
    def __init__(self, settings):
        self.settings = settings
        self._materials = {}

    @property
    def used_bytes(self):
        return sum(item.size_bytes for item in self._materials.values())

    def _reserve(self, size):
        if type(size) is not int or not 1 <= size <= self.settings.get("max_prefix_bytes", 64 * 1024 * 1024):
            raise ValueError("Prefix material size exceeds the configured bound")
        if len(self._materials) >= self.settings.get("max_prefix_handles", 32):
            raise PrefixCapacityError("Prefix handle capacity reached; release an existing handle")
        if self.used_bytes + size > self.settings.get("prefix_cache_bytes", 64 * 1024 * 1024):
            raise PrefixCapacityError("Prefix cache byte capacity reached; release an existing handle")

    def _get(self, handle, *, complete=None):
        if not isinstance(handle, str) or handle not in self._materials:
            raise ValueError("Unknown prefix handle (handles are local to this service process)")
        material = self._materials[handle]
        if complete is not None and material.complete != complete:
            raise ValueError("Prefix handle has the wrong import/completion state")
        return material

    @staticmethod
    def _info(handle, material):
        return {"handle": handle, "size_bytes": material.size_bytes,
                "sha256": material.sha256, "chunk_bytes": PREFIX_CHUNK_BYTES,
                "complete": material.complete}

    def put(self, payload):
        self._reserve(len(payload))
        handle = str(uuid.uuid4())
        material = Material(bytes(payload), hashlib.sha256(payload).hexdigest(), True)
        self._materials[handle] = material
        return self._info(handle, material)

    def read(self, handle, digest=None):
        material = self._get(handle, complete=True)
        if digest is not None and digest != material.sha256:
            raise ValueError("Prefix handle digest does not match its cached material")
        return bytes(material.data)

    def begin_prefix_import(self, size_bytes, sha256):
        self._reserve(size_bytes)
        if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        handle = str(uuid.uuid4())
        material = Material(bytearray(size_bytes), sha256, False)
        self._materials[handle] = material
        return self._info(handle, material)

    def release_prefix(self, handle):
        self._get(handle)
        del self._materials[handle]
        return {"released": True}

    def export_prefix_chunk(self, handle, offset=0):
        material = self._get(handle, complete=True)
        if type(offset) is not int or not 0 <= offset <= material.size_bytes:
            raise ValueError("Prefix offset is outside the material")
        data = material.data[offset:offset + PREFIX_CHUNK_BYTES]
        return {**self._info(handle, material), "offset": offset,
                "next_offset": offset + len(data), "data": np.frombuffer(data, dtype=np.uint8)}

    def import_prefix_chunk(self, handle, offset, data):
        material = self._get(handle, complete=False)
        data = np.asarray(data)
        if type(offset) is not int or offset != material.offset:
            raise ValueError("Prefix import chunks must be contiguous and ordered")
        if data.dtype != np.uint8 or data.ndim != 1 or not 0 < len(data) <= PREFIX_CHUNK_BYTES:
            raise ValueError("Prefix chunk must be uint8[N], 1 <= N <= 65536")
        end = offset + len(data)
        if end > material.size_bytes:
            raise ValueError("Prefix chunk exceeds declared material size")
        material.data[offset:end] = data.tobytes()
        material.offset = end
        return {"handle": handle, "next_offset": end, "size_bytes": material.size_bytes}

    def finish_prefix_import(self, handle):
        material = self._get(handle, complete=False)
        if material.offset != material.size_bytes:
            raise ValueError("Prefix import is incomplete")
        if hashlib.sha256(material.data).hexdigest() != material.sha256:
            raise ValueError("Imported prefix file checksum mismatch; release this handle")
        payload = bytes(material.data)
        parsed = validate_prefix(json.loads(payload))
        if len(parsed["steps"]) > self.settings.get("max_prefix_steps", 5000):
            raise ValueError("Imported prefix exceeds command capacity")
        material.data, material.complete = payload, True
        return self._info(handle, material)

    def close(self):
        self._materials.clear()
